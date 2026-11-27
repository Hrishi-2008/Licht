from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)
from typing import TypedDict
from langgraph.graph import StateGraph, START, END

from langchain_mistralai import ChatMistralAI
from langchain_google_genai import ChatGoogleGenerativeAI

from pydantic import BaseModel, Field
from typing import Literal
from typing import Annotated
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from langchain_tavily import TavilySearch
from langchain_core.messages import SystemMessage
from langchain_core.messages import HumanMessage
from langchain_core.messages import ToolMessage

from datetime import date

# Prompts
from routers.prompts import RESEARCH_PROMPT
from routers.prompts import Verifier_Prompt
from routers.prompts import WRITER_RULES
from routers.prompts import Evaluator_Prompt


# For Displaying messages at each Node
from langchain_core.runnables import RunnableConfig


def emit(config, message: str):
    """Send a status message to the endpoint, if one is listening."""
    fn = ((config or {}).get("configurable") or {}).get("emit")
    if fn:
        fn(message)
    else:
        print(f"[status] {message}")       # terminal runs just print it

def sub_config(config, limit: int) -> dict:
    """Pass the emit function down into a subgraph."""
    fn = ((config or {}).get("configurable") or {}).get("emit")
    return {"recursion_limit": limit, "configurable": {"emit": fn}}


# LangGraph Architecture Starts
class ReviewState(BaseModel):
    decision : Literal["good","bad"]
    reason : str
    suggestions : str

MAX_VERIFY_ATTEMPTS = 2

class ResearchState(TypedDict):
    research : str
    messages : Annotated[list, add_messages]
    sources : list[str]
    verified_status : str
    attempts : int

class ResearchOutput(BaseModel):
    research: str = Field(description="Summary of the key facts found")
    sources: list[str] = Field(description="URLs from the search results that support the facts")


class VerifierOutput(BaseModel):
    verified_status: Literal["yes", "no"]
    reason: str = Field(default="", description="If 'no', briefly say which claims are unsupported")


class State(TypedDict):
    topic : str
    research : str
    sources : list[str]
    report : str
    review : ReviewState | None
    next : str
    verified_status : str
    error : str | None

# llm = ChatMistralAI(
#     model = "ministral-14b-2512",
#     temperature = 0 
# )
llm = ChatGoogleGenerativeAI(
    model = "gemini-3.1-flash-lite",
    temperature = 0 
)

def SupervisingRouter(state : State):
    s = ""
    if(state["error"] is not None):
        s = "error_handler"
    elif(state["research"]==""):
        s = "researcher"
    elif(state["report"]==""):
        s = "reportGen"
    else: 
        s = "end"
    return {
        "next" : s
    }

def route(state : State):
    return state["next"]


# Research Agent
searchWeb = TavilySearch(max_results=5)
tool_node = ToolNode([searchWeb])

llm_with_tools = llm.bind_tools([searchWeb])            # for searching
extractor_llm = llm.with_structured_output(ResearchOutput)   # for the final answer
verifier_llm = llm.with_structured_output(VerifierOutput)

today = date.today()


def researcherNode(reState: ResearchState, config: RunnableConfig):

    if not isinstance(reState["messages"][-1], ToolMessage):
        if reState.get("attempts", 0) == 0:
            emit(config, "Scraping the web for facts...")
        else:
            emit(config, "Scraping the web for more facts...")

    response = llm_with_tools.invoke(
        [SystemMessage(RESEARCH_PROMPT), *reState["messages"]]
    )

    # the model wants to search: let the tool node run, then come back here
    if response.tool_calls:
        return {"messages": [response]}

    # no more tool calls: turn the written summary into structured output
    structured = extractor_llm.invoke([
        SystemMessage("Extract the final research summary and the source URLs from the conversation."),
        *reState["messages"],
        response,
        HumanMessage("Return the final research summary and the list of source URLs used."),
    ])

    return {
        "messages": [response],
        "research": structured.research,
        "sources": structured.sources,
    }



def CallTool(reState: ResearchState):
    if reState["messages"] and reState["messages"][-1].tool_calls:
        return "tools"
    return "verify"



def Verifier(reState: ResearchState, config: RunnableConfig):
    # the verifier must see the actual search results, not just the URLs
    emit(config, "Verifying the scraped facts...")
    evidence = "\n\n".join(
        str(m.content)[:12000] for m in reState["messages"] if isinstance(m, ToolMessage)
    )

    result = verifier_llm.invoke([
        SystemMessage(Verifier_Prompt),
        HumanMessage(
            f"Research notes:\n{reState['research']}\n\n"
            f"Listed sources:\n{reState['sources']}\n\n"
            f"Search results:\n{evidence}"
        ),
    ])

    update = {
        "verified_status": result.verified_status,
        "attempts": reState.get("attempts", 0) + 1,
    }

    # give the researcher feedback so the retry is not a blind repeat
    if result.verified_status == "no":
        update["messages"] = [HumanMessage(
            f"Verification failed. Unsupported claims: {result.reason}\n"
            "Run NEW searches specifically for these claims. Remove any claim you cannot "
            "find in the search results."
        )]
    return update


def verRouter(reState: ResearchState):
    if reState["verified_status"] == "yes":
        return "done"
    if reState["attempts"] >= MAX_VERIFY_ATTEMPTS:
        return "done"              # give up after N tries and return the best effort
    return "retry"


Subgraph = StateGraph(ResearchState)

Subgraph.add_node("researcher", researcherNode)
Subgraph.add_node("tools", tool_node)
Subgraph.add_node("verifier", Verifier)

Subgraph.add_edge(START, "researcher")
Subgraph.add_conditional_edges(
    "researcher", CallTool,
    {"tools": "tools", "verify": "verifier"},
)
Subgraph.add_edge("tools", "researcher")
Subgraph.add_conditional_edges(
    "verifier", verRouter,
    {"done": END, "retry": "researcher"},
)

research_app = Subgraph.compile()


# ---------- the node used by the main graph ----------

def researcher(state: State, config: RunnableConfig):
    try:
        result = research_app.invoke(
            {
                "messages": [
                    HumanMessage(
                        content=f"Topic is {state['topic']}"
                    )
                ],
                "research": "",
                "sources": [],
                "verified_status": "pending",
                "attempts": 0,
            },
            config=sub_config(config,40),
        )

        status = result["verified_status"]

        if not result["research"].strip():
            raise ValueError("Researcher returned empty research.")
        
        if not result["sources"]:
            raise ValueError(
                "Researcher returned no source URLs."
            )

        research = result["research"]
        if status != "yes":
                research = "NOTE: This research could not be fully verified.\n\n" + research

        return {
            "research": research,
            "sources": result["sources"],
            "verified_status": status,
            "error": None,
        }

    except Exception as e:
        return {
            "research": "",
            "sources": [],
            "verified_status": "no",
            "error": f"Research failed: {e}",
        }



# Combining writer and reviewer into a subgraph.
class repGenState(TypedDict):
    topic : str
    research : str
    report : str
    review : ReviewState | None
    count : int
    error : str | None


def writer(repstate: repGenState, config: RunnableConfig):
    try:

        if repstate["review"] is None:
            emit(config, "Generating the report...")
            messages = [
                SystemMessage(WRITER_RULES),
                HumanMessage(f"Here is the topic : {repstate['topic']}, and research : {repstate['research']}, write a good report based on the research.")
            ]
        else:
            emit(config, "Refining the report...")
            messages = [
                SystemMessage(WRITER_RULES),
                HumanMessage(f"""
                    research : {repstate['research']},
                    previous report : {repstate['report']},
                    reason of rejection : {repstate['review'].reason},
                    suggestions of improvement : {repstate['review'].suggestions},
                    From these, improve the previous report.
                """)
            ]
        response = llm.invoke(messages)

        content = response.content

        if isinstance(content, list):
            content = content[0]["text"]

        if not content or not content.strip():
            raise ValueError("Writer returned an empty report.")

        return {"report": content, "review": None, "error": None}
    
    except Exception as e:
        return {"report": "", "error": f"An error has occured in writer node =>\nError : {e}"}


def writer_router(repstate: repGenState):
    return "end" if repstate.get("error") else "reviewer"


def reviewer(repstate: repGenState, config: RunnableConfig):
    try:
        emit(config, "Reviewing the report and verifying the evidence...")
        reviewer_llm = llm.with_structured_output(ReviewState)
        
        response = reviewer_llm.invoke([
            SystemMessage(Evaluator_Prompt),
            HumanMessage(f"Research:\n{repstate['research']}\n\nReport:\n{repstate['report']}")
        ])
        return {"review": response, "count": repstate["count"] + 1}
    except Exception:
        # non-fatal: keep the report we already have
        return {"review": None, "count": repstate["count"] + 1}


def decider(repstate: repGenState):
    review = repstate.get("review")
    if review is None:
        return "end"
    if review.decision == "bad" and repstate["count"] < 3:
        return "writer"
    return "end"


G = StateGraph(repGenState)
G.add_node("writer", writer)
G.add_node("reviewer", reviewer)
G.add_edge(START, "writer")
G.add_conditional_edges("writer", writer_router, {"reviewer": "reviewer", "end": END})
G.add_conditional_edges("reviewer", decider, {"writer": "writer", "end": END})
report_app = G.compile()


def reportGen(state: State, config: RunnableConfig):
    result = report_app.invoke({
        "topic": state["topic"],
        "research": state["research"],
        "report": "",
        "review": None,
        "count": 0,
        "error": None,
    },
    config=sub_config(config, 25))

    report = result.get("report", "")
    if report and state["sources"]:
        report += "\n\n## Sources\n" + "\n".join(f"- {u}" for u in state["sources"])

    return {
        "report": report,
        "review": result.get("review"),
        "error": result.get("error"),
    }


# Error Handler
def error_handler(state : State):
    print(f"The following error occured :\n{state['error']}")
    return {"error" : state["error"]}


graph = StateGraph(State)

graph.add_node("SupervisingRouter",SupervisingRouter)
graph.add_node("researcher",researcher)
graph.add_node("reportGen",reportGen)
graph.add_node("error_handler",error_handler)

graph.add_edge(START,"SupervisingRouter")
graph.add_conditional_edges(
    "SupervisingRouter",
    route,
    {
        "researcher" : "researcher",
        "reportGen" : "reportGen",
        "error_handler" : "error_handler",
        "end" : END
    }
)
graph.add_edge("researcher","SupervisingRouter")
graph.add_edge("reportGen","SupervisingRouter")
graph.add_edge("error_handler",END)

app = graph.compile()

# FastAPI
from fastapi import APIRouter
from fastapi import HTTPException

router = APIRouter()

import json
from fastapi.responses import StreamingResponse

import json, queue, threading
from fastapi.responses import StreamingResponse

def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"

@router.get("/getReport")
def get_report_stream(topic: str):
    q: queue.Queue = queue.Queue()

    def emit_status(message: str):
        q.put({"type": "status", "message": message})

    def worker():
        try:
            result = app.invoke(
                {
                    "topic": topic, "research": "", "report": "", "review": None,
                    "next": "", "verified_status": "", "error": None, "sources": [],
                },
                config={"recursion_limit": 30, "configurable": {"emit": emit_status}},
            )
            if result.get("error"):
                q.put({"type": "error", "message": result["error"]})
            else:
                q.put({"type": "report", "report": result["report"]})
        except Exception as e:
            q.put({"type": "error", "message": str(e)})
        finally:
            q.put(None)                     # tells the stream to finish

    threading.Thread(target=worker, daemon=True).start()

    def event_stream():
        while True:
            try:
                item = q.get(timeout=15)
            except queue.Empty:
                yield ": keepalive\n\n"     # ignored by the browser, keeps the connection open
                continue
            if item is None:
                break
            yield sse(item)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
