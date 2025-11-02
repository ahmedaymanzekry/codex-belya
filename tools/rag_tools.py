import logging
import os
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

from livekit.agents import RunContext, function_tool
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter


logger = logging.getLogger(__name__)


_EXCLUDED_DIRECTORIES = {
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "logs",
    "dist",
    "build",
    "coverage",
}

_ALLOWED_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".rst",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
}


class RAGFunctionToolsMixin:
    """Mixin exposing retrieval-augmented generation helpers for repository research."""

    _rag_splitter = RecursiveCharacterTextSplitter(
        separators=["\n\n", "\n", " ", ""],
        chunk_size=1200,
        chunk_overlap=200,
    )

    def _rag_state(self) -> Dict[str, object]:
        """Return or initialize the in-memory RAG index state."""
        state = getattr(self, "_rag_state_cache", None)
        if state is None:
            state = {
                "documents": [],  # type: ignore[list]
                "fingerprint": {},  # type: ignore[dict]
                "root": Path(os.getcwd()),
            }
            setattr(self, "_rag_state_cache", state)
        return state

    def _rag_is_excluded(self, path: Path) -> bool:
        if any(part in _EXCLUDED_DIRECTORIES for part in path.parts):
            return True
        return False

    def _rag_candidate_files(self, root: Path) -> Dict[str, float]:
        """Collect repository files that should participate in retrieval."""
        file_index: Dict[str, float] = {}
        for candidate in root.rglob("*"):
            if not candidate.is_file():
                continue
            if self._rag_is_excluded(candidate):
                continue
            suffix = candidate.suffix.lower()
            if suffix not in _ALLOWED_SUFFIXES and suffix != "":
                continue
            try:
                stat_result = candidate.stat()
            except OSError as error:
                logger.debug("Skipping %s due to stat error: %s", candidate, error)
                continue
            if stat_result.st_size == 0:
                continue
            if stat_result.st_size > 256_000:
                logger.debug("Skipping %s because it exceeds the 256KB chunking limit.", candidate)
                continue
            file_index[str(candidate)] = stat_result.st_mtime
        return file_index

    def _rag_build_documents(self, file_index: Dict[str, float], root: Path) -> List[Document]:
        """Create LangChain documents for repository files."""
        documents: List[Document] = []
        for file_path in sorted(file_index.keys()):
            path = Path(file_path)
            try:
                raw_text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                raw_text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError as error:
                logger.debug("Skipping %s due to read error: %s", file_path, error)
                continue

            if not raw_text.strip():
                continue

            rel_path = os.path.relpath(path, root)
            base_document = Document(page_content=raw_text, metadata={"source": rel_path})
            chunks = self._rag_splitter.split_documents([base_document])
            for chunk_index, chunk in enumerate(chunks):
                chunk.metadata.setdefault("source", rel_path)
                chunk.metadata["chunk_index"] = chunk_index
                documents.append(chunk)
        return documents

    def _rag_documents(self) -> List[Document]:
        """Return the cached LangChain documents, rebuilding if necessary."""
        state = self._rag_state()
        root = state["root"]
        if not isinstance(root, Path):
            root = Path(os.getcwd())
            state["root"] = root

        fingerprint = self._rag_candidate_files(root)
        cached_fingerprint = state.get("fingerprint") or {}

        if fingerprint != cached_fingerprint:
            logger.info("Rebuilding RAG cache; detected %d repository files.", len(fingerprint))
            documents = self._rag_build_documents(fingerprint, root)
            state["documents"] = documents
            state["fingerprint"] = fingerprint
        return state["documents"]  # type: ignore[return-value]

    @staticmethod
    def _rag_tokenize(query: str) -> List[str]:
        tokens = re.split(r"\W+", query.lower())
        return [token for token in tokens if token]

    @staticmethod
    def _rag_score_document(tokens: Sequence[str], document: Document) -> float:
        lowered = document.page_content.lower()
        if not lowered.strip():
            return 0.0
        keyword_hits = sum(lowered.count(token) for token in tokens)
        seq_ratio = SequenceMatcher(None, " ".join(tokens), lowered).ratio()
        return keyword_hits * 3 + seq_ratio

    def _rag_rank_documents(self, query: str, documents: Iterable[Document], limit: int) -> List[Tuple[Document, float]]:
        tokens = self._rag_tokenize(query)
        if not tokens:
            return []
        scored: List[Tuple[Document, float]] = []
        for document in documents:
            score = self._rag_score_document(tokens, document)
            if score > 0:
                scored.append((document, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:limit]

    @staticmethod
    def _rag_format_snippet(document: Document, max_length: int = 320) -> str:
        snippet = document.page_content.strip().replace("\n", " ").replace("  ", " ")
        if len(snippet) > max_length:
            snippet = snippet[: max_length - 3].rstrip() + "..."
        return snippet

    def _rag_build_response(self, query: str, ranked_documents: Sequence[Tuple[Document, float]]) -> str:
        if not ranked_documents:
            return (
                f"I checked the repository for '{query}' but did not find any relevant snippets. "
                "Consider refining the question with specific filenames or keywords."
            )

        lines = [
            f"I inspected the codebase for '{query}'. Here are the most relevant findings:",
        ]
        for document, score in ranked_documents:
            source = document.metadata.get("source", "unknown file")
            snippet = self._rag_format_snippet(document)
            lines.append(f"- {source} (score {score:.2f}): {snippet}")
        lines.append("Let me know if you want a deeper dive into any of these files or topics.")
        return "\n".join(lines)

    @function_tool
    async def research_repository(self, question: str, run_ctx: RunContext, max_snippets: int = 5) -> str:
        """Search the local repository with a lightweight RAG pipeline and summarize relevant snippets."""
        try:
            documents = self._rag_documents()
            ranked = self._rag_rank_documents(question, documents, max(1, min(max_snippets, 8)))
            return self._rag_build_response(question, ranked)
        except Exception as error:  # pragma: no cover - handled through shared helper
            return self._handle_tool_error("researching the repository", error)

