from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langchain_mistralai import ChatMistralAI

from pydantic import BaseModel
from typing import Literal
from typing import Annotated
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from langchain_tavily import TavilySearch
from langchain_core.messages import SystemMessage
from langchain_core.messages import HumanMessage


class ReviewState(BaseModel):
    decision : Literal["good","bad"]
    reason : str
    suggestions : str

class ResearchState(TypedDict):
    research : str
    messages : Annotated[list, add_messages]

class State(TypedDict):
    topic : str
    research : str
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

def router(state : State):
    return state["next"]


# Research Subgraph
def researcher(state : State):
    try:
        searchWeb = TavilySearch(max_results=5)
        tool_node = ToolNode([searchWeb])

        def researcherNode(reState : ResearchState):
            llm_with_tools = llm.bind_tools([searchWeb])

            message = [
                SystemMessage("You are an expert researcher AI. Use the tavily_search tool to gather facts on the topic. Once you have search results, stop searching and write a summary of the key facts."),
                *reState["messages"]
            ]
            response = llm_with_tools.invoke(message)

            if(response.tool_calls):
                return {
                    "messages" : [response]
                }
        
            return {
                "messages" : [response],
                "research" : response.content
            }

        def CallTool(reState : ResearchState):
            if(reState["messages"][-1].tool_calls):
                return "tools"
            return "end"

        Subgraph = StateGraph(ResearchState)

        Subgraph.add_node("researcher",researcherNode)
        Subgraph.add_node("tool_node",tool_node)

        Subgraph.add_edge(START,"researcher")
        Subgraph.add_conditional_edges(
            "researcher",
            CallTool,
            {
                "tools" : "tool_node",
                "end" : END
            }
        )
        Subgraph.add_edge("tool_node","researcher")

        research_app = Subgraph.compile()

        result = research_app.invoke(
            {
                "messages" : [
                    HumanMessage(content=f"Topic is {state['topic']}")
                ],
                "research" : ""
            },
            config={"recursion_limit" : 30}
        )

        return {
            "research" : result["research"],
            "error" : None
        }
    except Exception as e:
        return {
            "research" : "",
            "error" : f"An error has occured in researcher node =>\nError : {e}"
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
    router,
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
                    "review": None, "next": "", "error": None
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
