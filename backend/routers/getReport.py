from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langchain_mistralai import ChatMistralAI

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


class ReviewState(BaseModel):
    decision : Literal["good","bad"]
    reason : str
    suggestions : str

MAX_VERIFY_ATTEMPTS = 1

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
    count : int
    next : str
    error : str | None

llm = ChatMistralAI(
    model = "ministral-14b-2512",
    temperature = 0 
)

def SupervisingRouter(state : State):
    s = ""
    if(state["error"] is not None):
        s = "error_handler"
    elif(state["research"]==""):
        s = "researcher"
    elif(state["report"]==""):
        s = "writer"
    elif(state["review"] is None):
        s = "reviewer"
    elif(state["review"].decision=="bad" and state["count"]<3):
        s = "writer"
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

RESEARCH_PROMPT = (
    f"Today's date is {today:%B %d, %Y}. Your training data is older than this, so do not "
    "assume any particular year is the current one. "
    "You are an expert researcher AI. Use the tavily_search tool to gather facts on the topic. "
    "Match the scope of your research to the topic. If the topic is broad (a subject, field, "
    "concept, sport, organism, technology), research the whole subject: what it is, its history, "
    "how it works or its rules, key concepts, notable people or events, and, where relevant, "
    "its current state. Do NOT add a year to your search queries and do NOT turn the research "
    "into a news roundup. Only focus on a particular year or period if the topic itself asks for "
    "it (for example 'latest', 'current', 'this year', or a specific year). "
    "Run several searches, each covering a different aspect. "
    "Any fact that can change over time (rankings, current champions, prices, officeholders, "
    "records, statistics) must be searched for specifically and stated with its date or year "
    "from the source. Never fill gaps from memory; use only the search results. "
    "Once you have enough results, write a summary of the key facts organized by aspect, and "
    "keep the source URLs."
)


def researcherNode(reState: ResearchState):
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
    if reState["messages"][-1].tool_calls:
        return "tools"
    return "verify"


def Verifier(reState: ResearchState):
    # the verifier must see the actual search results, not just the URLs
    evidence = "\n\n".join(
        str(m.content) for m in reState["messages"] if isinstance(m, ToolMessage)
    )[:20000]

    result = verifier_llm.invoke([
        SystemMessage(
            "You are a strict fact verifier. You get research notes and the raw search results "
            "they were based on. Answer 'yes' only if every claim in the notes is supported by the "
            "search results and every listed source appears in them. Otherwise answer 'no' and "
            "explain briefly in 'reason'."
        ),
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
            f"Verification failed: {result.reason}. Search again if needed, and keep only "
            f"claims that are supported by the search results."
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
def researcher(state: State):
    try:
        result = research_app.invoke(
            {
                "messages": [HumanMessage(content=f"Topic is {state['topic']}")],
                "research": "",
                "sources": [],
                "verified_status": "",
                "attempts": 0,
            },
            config={"recursion_limit": 40},
        )

        return {
            "research": result["research"],
            "sources": result["sources"],
            "error": None,
        }
    except Exception as e:
        return {
            "research": "",
            "sources": [],
            "error": f"An error has occurred in researcher node =>\nError : {e}",
        }


# Writer Node
def writer(state : State):
    try:
        if(state["review"] is None):
            messages = [
                SystemMessage("You are an excellent report writer that can analyze the research facts given and write a perfect report."),
                HumanMessage(f"Here is the research : {state['research']}, write a good report based on the research.")
            ]
        else:
            messages = [
                SystemMessage("You are an excellent report writer that can understand the reason for rejection of previous report and suggestions where the previous report can be improved."),
                HumanMessage(f"""
                    previous report : {state['report']},
                    reason of rejection : {state['review'].reason},
                    suggestions of improvement : {state['review'].suggestions},
                    From these, improve the previous report.
                """)
            ]

        response = llm.invoke(messages)

        return {
            "report" : response.content,
            "review" : None,
            "error" : None
        }
    except Exception as e:
        return {
            "report" : "",
            "error" : f"An error has occured in writer node =>\nError : {e}"
        }


# Review Node
def reviewer(state : State):
    try :
        llm_with_structured_output = llm.with_structured_output(ReviewState)
        messages = [
            SystemMessage("You are a brutally honest critique that can analyze a report and review if it good or bad, and also if the report is bad, give reason for it and the suggestions where the report can be improved."),
            HumanMessage(f"Here is the report : {state['report']}, I want your brutally honest opinion and also reasons and suggestions if the report is bad.")
        ]

        response = llm_with_structured_output.invoke(messages)

        return {
            "review" : response,
            "count" : state["count"] + 1,
            "error" : None
        }
    except Exception as e:
        return {
            "review" : None,
            "error" : f"An error has occured in reviewer node =>\nError : {e}"
        }

def error_handler(state : State):
    print(f"The following error occured :\n{state['error']}")
    return {}


graph = StateGraph(State)

graph.add_node("SupervisingRouter",SupervisingRouter)
graph.add_node("researcher",researcher)
graph.add_node("writer",writer)
graph.add_node("reviewer",reviewer)
graph.add_node("error_handler",error_handler)

graph.add_edge(START,"SupervisingRouter")
graph.add_conditional_edges(
    "SupervisingRouter",
    route,
    {
        "researcher" : "researcher",
        "writer" : "writer",
        "reviewer" : "reviewer",
        "error_handler" : "error_handler",
        "end" : END
    }
)
graph.add_edge("researcher","SupervisingRouter")
graph.add_edge("writer","SupervisingRouter")
graph.add_edge("reviewer","SupervisingRouter")
graph.add_edge("error_handler",END)

app = graph.compile()


# FastAPI
from fastapi import APIRouter
from fastapi import HTTPException

router = APIRouter()

import json
from fastapi.responses import StreamingResponse

STEP_MESSAGES = {
    "researcher": "Scraping the web for facts...",
    "writer": "Generating the report...",
    "reviewer": "Verifying the evidence...",
}

def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"

@router.get("/getReport")
def get_report_stream(topic: str):
    def event_stream():
        report = ""
        try:
            for chunk in app.stream(
                {
                    "topic": topic, "count": 0, "research": "", "report": "",
                    "review": None, "next": "", "error": None, "sources" : []
                },
                config={"recursion_limit": 30},
                stream_mode="updates",
            ):
                for node, update in chunk.items():
                    if not update:
                        continue
                    if node == "SupervisingRouter" and update.get("next") in STEP_MESSAGES:
                        yield sse({"type": "status", "message": STEP_MESSAGES[update["next"]]})
                    if update.get("report"):
                        report = update["report"]
                    if update.get("error"):
                        yield sse({"type": "error", "message": update["error"]})
                        return
            yield sse({"type": "report", "report": report})
        except Exception as e:
            yield sse({"type": "error", "message": str(e)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
