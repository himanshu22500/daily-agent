"""Docs Q&A agent — ask a natural question, get an answer from Outline.

Unlike the general `assistant`, this agent is docs-first: given a
how-to / setup / "how does X work" question, it searches the Outline knowledge
base, reads the most relevant documents in full, and synthesizes a concrete,
step-by-step answer grounded in — and citing — those docs.
"""

from __future__ import annotations

from pydantic_ai import Agent, RunContext

from ..sources.outline import OutlineClient, OutlineError
from .model import build_model, cache_settings

_SYSTEM_PROMPT = """\
You help engineers by answering their questions from the company's Outline
knowledge base (PRDs, ERDs, SOWs, TDDs, workflow docs, runbooks).

Process:
  1. Search the docs for the question's key terms. Try a couple of phrasings if
     the first search is thin.
  2. Read the most relevant documents IN FULL with read_doc before answering —
     don't answer from search snippets alone.
  3. Synthesize a clear, actionable answer. For "how do I set up / do X"
     questions, give concrete numbered steps in order. Include commands, config,
     env vars, and prerequisites exactly as the docs state them.

Rules:
  - Ground everything in the docs. Cite the document title (and URL) you drew
    each part from.
  - If the docs partially cover it, answer what you can and clearly flag the
    gaps rather than inventing steps.
  - If nothing relevant exists, say so and list the closest docs you found.
  - Be concise and practical — the reader wants to get the task done.
"""


def build_docs_agent(model) -> Agent[OutlineClient, str]:
    agent = Agent(
        model,
        deps_type=OutlineClient,
        system_prompt=_SYSTEM_PROMPT,
        model_settings=cache_settings(model if isinstance(model, str) else ""),
    )

    @agent.tool
    async def search_docs(ctx: RunContext[OutlineClient], query: str) -> str:
        """Search the Outline knowledge base. Returns titles, ids, and snippets."""
        try:
            results = await ctx.deps.search(query, limit=8)
        except OutlineError as e:
            return f"(Outline error: {e})"
        if not results:
            return f"(no docs found for '{query}')"
        return "\n".join(
            f"- {r['title']} (id: {r['id']})\n    {' '.join((r['context'] or '').split())[:200]}"
            for r in results
        )

    @agent.tool
    async def read_doc(ctx: RunContext[OutlineClient], doc_id: str) -> str:
        """Read a document's full content by its id (from search_docs)."""
        try:
            doc = await ctx.deps.read_document(doc_id)
        except OutlineError as e:
            return f"(Outline error: {e})"
        return f"# {doc['title']}\n({doc['url']})\n\n{doc['text']}"

    return agent


async def ask_docs(model, outline: OutlineClient, question: str) -> str:
    agent = build_docs_agent(build_model(model))
    result = await agent.run(question, deps=outline)
    return result.output
