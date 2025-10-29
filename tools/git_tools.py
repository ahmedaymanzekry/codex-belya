import logging
import os
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from git import Repo
from git.exc import GitCommandError
from livekit.agents import function_tool


logger = logging.getLogger(__name__)

StatusEntry = Tuple[str, str]
DiffEntry = Tuple[str, str, str]


def _describe_status(code: str) -> str:
    mapping = {
        "M": "modified",
        "A": "added",
        "D": "deleted",
        "R": "renamed",
        "C": "copied",
        "U": "updated",
        "T": "type changed",
        "?": "untracked",
        "!": "ignored",
    }
    return mapping.get(code, code)


def _parse_short_status(output: str) -> Dict[str, List[StatusEntry]]:
    staged: List[StatusEntry] = []
    unstaged: List[StatusEntry] = []
    untracked: List[StatusEntry] = []
    ignored: List[StatusEntry] = []

    for raw in output.splitlines():
        if not raw:
            continue
        if raw.startswith("??"):
            path = raw[3:].strip()
            untracked.append((path, "untracked"))
            continue
        if raw.startswith("!!"):
            path = raw[3:].strip()
            ignored.append((path, "ignored"))
            continue
        if len(raw) < 3:
            continue
        index_status = raw[0]
        worktree_status = raw[1]
        path = raw[3:].strip()

        if index_status != " " and index_status != "?":
            staged.append((path, _describe_status(index_status)))
        if worktree_status not in (" ", "?"):
            untranslated = _describe_status(worktree_status)
            unstaged.append((path, untranslated))
        if worktree_status == "?":
            untracked.append((path, "untracked"))
        if worktree_status == "!":
            ignored.append((path, "ignored"))

    return {
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "ignored": ignored,
    }


def _parse_numstat(output: str) -> List[DiffEntry]:
    entries: List[DiffEntry] = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            entries.append((parts[0], parts[1], parts[2]))
    return entries


def _format_status_list(entries: Iterable[StatusEntry]) -> str:
    formatted = [f"{path} ({description})" for path, description in entries]
    return ", ".join(formatted)


def _format_diff_entries(entries: Iterable[DiffEntry]) -> str:
    formatted: List[str] = []
    for added, removed, path in entries:
        if added == "-" or removed == "-":
            change_summary = "binary change"
        else:
            change_summary = f"+{added}/-{removed}"
        formatted.append(f"{path} ({change_summary})")
    return ", ".join(formatted)


class GitFunctionToolsMixin:
    """Mixin that provides git-related function tools for the Codex agent."""

    def _repo(self) -> Repo:
        repo_path = os.getcwd()
        return Repo(repo_path)

    @function_tool
    async def status(self) -> str:
        """Summarize the current git status including staged and unstaged files."""
        try:
            repo = self._repo()
            if getattr(repo.head, "is_detached", False):
                current_branch = "a detached HEAD"
            else:
                current_branch = repo.active_branch.name

            short_output = repo.git.status("--short")
            status_sections = _parse_short_status(short_output)

            staged = status_sections["staged"]
            unstaged = status_sections["unstaged"]
            untracked = status_sections["untracked"]
            ignored = status_sections["ignored"]

            if not (staged or unstaged or untracked):
                logger.info("git status: clean working tree on %s", current_branch)
                return f"The working tree on {current_branch} is clean with no staged or pending changes."

            messages: List[str] = [f"On {current_branch}."]
            if staged:
                messages.append(f"Staged changes: {_format_status_list(staged)}.")
            else:
                messages.append("No staged changes.")

            if unstaged:
                messages.append(f"Unstaged changes: {_format_status_list(unstaged)}.")
            else:
                messages.append("No unstaged changes.")

            if untracked:
                untracked_list = ", ".join(path for path, _ in untracked)
                messages.append(f"Untracked files: {untracked_list}.")

            if ignored:
                ignored_list = ", ".join(path for path, _ in ignored)
                messages.append(f"Ignored files (left untouched): {ignored_list}.")

            summary = " ".join(messages)
            logger.info("git status summary: %s", summary)
            return summary
        except Exception as error:  # pragma: no cover - handled via _handle_tool_error
            return self._handle_tool_error("checking git status", error)

    @function_tool
    async def add(self, paths: Optional[Sequence[str]] = None) -> str:
        """Stage specific paths or everything when no paths are provided."""
        try:
            repo = self._repo()
            if not paths:
                repo.git.add(all=True)
                logger.info("Staged all changes in repository %s", repo.working_dir)
                return "Staged all tracked and untracked changes."

            normalized_paths = [path.strip() for path in paths if path.strip()]
            if not normalized_paths:
                return "No paths were provided to stage."

            repo.git.add(*normalized_paths)
            staged_list = ", ".join(normalized_paths)
            logger.info("Staged paths: %s", staged_list)
            return f"Staged the following paths: {staged_list}."
        except Exception as error:  # pragma: no cover - handled via _handle_tool_error
            return self._handle_tool_error("staging changes", error)

    @function_tool
    async def diff(self) -> str:
        """Summarize staged and unstaged diffs with line change counts and patches."""
        try:
            repo = self._repo()
            staged_output = repo.git.diff("--cached", "--numstat")
            unstaged_output = repo.git.diff("--numstat")
            staged_patch = repo.git.diff("--cached")
            unstaged_patch = repo.git.diff()

            staged_entries = _parse_numstat(staged_output)
            unstaged_entries = _parse_numstat(unstaged_output)
            untracked_files = repo.untracked_files

            messages: List[str] = []
            if staged_entries:
                messages.append(f"Staged changes: {_format_diff_entries(staged_entries)}.")
            else:
                messages.append("No staged diffs.")

            if unstaged_entries:
                messages.append(f"Unstaged changes: {_format_diff_entries(unstaged_entries)}.")
            else:
                messages.append("No unstaged diffs.")

            if untracked_files:
                untracked_list = ", ".join(untracked_files)
                messages.append(f"Untracked files (not part of the diff): {untracked_list}.")

            if not staged_entries and not unstaged_entries and not untracked_files:
                logger.info("git diff: no changes detected")
                return "There are no staged or unstaged diffs; the working tree is clean."

            diff_details: List[str] = []
            if staged_patch.strip():
                diff_details.append("Staged diff:\n" + staged_patch.strip())
            if unstaged_patch.strip():
                diff_details.append("Unstaged diff:\n" + unstaged_patch.strip())

            summary = " ".join(messages)
            logger.info("git diff summary: %s", summary)
            if diff_details:
                detail_block = "\n\n".join(diff_details)
                return f"{summary}\n\n{detail_block}"
            return summary
        except Exception as error:  # pragma: no cover - handled via _handle_tool_error
            return self._handle_tool_error("summarizing diffs", error)

    @function_tool
    async def restore(self, paths: Optional[Sequence[str]] = None, unstage: bool = False) -> str:
        """Restore files by discarding working tree changes or unstaging them."""
        try:
            repo = self._repo()
            target_paths = [path.strip() for path in (paths or ["."]) if path.strip()]
            if not target_paths:
                return "No paths were provided to restore."

            if unstage:
                repo.git.restore("--staged", *target_paths)
                restored = ", ".join(target_paths)
                logger.info("Unstaged paths: %s", restored)
                return f"Unstaged the following paths: {restored}."

            repo.git.restore("--worktree", "--source=HEAD", *target_paths)
            restored = ", ".join(target_paths)
            logger.info("Discarded worktree changes for paths: %s", restored)
            return f"Discarded local modifications for: {restored}."
        except GitCommandError as error:
            if "untracked" in (error.stderr or "").lower():
                return (
                    "Git could not restore untracked files. "
                    "Please remove them manually if you want to discard them."
                )
            return self._handle_tool_error("restoring files", error)
        except Exception as error:  # pragma: no cover - handled via _handle_tool_error
            return self._handle_tool_error("restoring files", error)

    @function_tool
    async def reset(
        self,
        commit: Optional[str] = None,
        paths: Optional[Sequence[str]] = None,
        mode: str = "mixed",
    ) -> str:
        """Reset the current branch or unstage specific paths."""
        try:
            repo = self._repo()
            commitish = commit or "HEAD"

            if paths:
                normalized_paths = [path.strip() for path in paths if path.strip()]
                if not normalized_paths:
                    return "No paths were provided to reset."
                repo.git.reset(commitish, *normalized_paths)
                staged_list = ", ".join(normalized_paths)
                logger.info("Unstaged paths %s back to %s", staged_list, commitish)
                return f"Unstaged {staged_list} back to {commitish}."

            allowed_modes = {"soft", "mixed", "hard", "keep", "merge"}
            normalized_mode = mode.lower() if mode else "mixed"
            if normalized_mode not in allowed_modes:
                allowed = ", ".join(sorted(allowed_modes))
                return f"Reset mode '{mode}' is not supported. Please use one of: {allowed}."

            args: List[str] = [f"--{normalized_mode}"]
            if commit:
                args.append(commit)
            repo.git.reset(*args)
            commit_target = commit or "the current HEAD"
            logger.info("Performed git reset --%s %s", normalized_mode, commit_target)
            return f"Reset the current branch with --{normalized_mode} to {commit_target}."
        except Exception as error:  # pragma: no cover - handled via _handle_tool_error
            return self._handle_tool_error("resetting changes", error)

    @function_tool
    async def stash(
        self,
        action: str = "push",
        message: Optional[str] = None,
        stash_ref: Optional[str] = None,
        include_untracked: bool = False,
    ) -> str:
        """Manage git stash entries (push, list, pop, apply, drop, or clear)."""
        try:
            repo = self._repo()
            normalized_action = action.lower().strip() if action else "push"
            stash_arguments: List[str]

            if normalized_action in {"push", "save"}:
                stash_arguments = ["push"]
                if include_untracked:
                    stash_arguments.append("--include-untracked")
                if message:
                    stash_arguments.extend(["-m", message])
                output = repo.git.stash(*stash_arguments)
                logger.info("Created stash entry: %s", output.strip())
                return f"Created a new stash entry. Git replied: {output.strip()}"

            if normalized_action == "list":
                output = repo.git.stash("list")
                response = output.strip() or "No stash entries found."
                logger.info("Stash list retrieved")
                return response

            if normalized_action in {"pop", "apply", "drop"}:
                target = stash_ref or "stash@{0}"
                output = repo.git.stash(normalized_action, target)
                logger.info("Performed stash %s on %s", normalized_action, target)
                return f"Ran `git stash {normalized_action} {target}`. Git replied: {output.strip()}"

            if normalized_action == "clear":
                repo.git.stash("clear")
                logger.info("Cleared all stash entries")
                return "Cleared all stash entries."

            return (
                "Unsupported stash action. Please use push, list, pop, apply, drop, or clear."
            )
        except Exception as error:  # pragma: no cover - handled via _handle_tool_error
            return self._handle_tool_error("running git stash", error)

    @function_tool
    async def merge(
        self,
        source_branch: str,
        target_branch: Optional[str] = None,
        no_fast_forward: bool = False,
        squash: bool = False,
    ) -> str:
        """Merge a source branch into the current (or specified target) branch."""
        try:
            repo = self._repo()
            current_branch = repo.active_branch.name
            destination = target_branch or current_branch

            if destination != current_branch:
                return (
                    f"You are currently on {current_branch}. Please switch to {destination} before merging."
                )

            available = [head.name for head in repo.heads]
            if source_branch not in available:
                return f"The branch {source_branch} does not exist locally."

            if source_branch == destination:
                return "Source and target branches are the same; nothing to merge."

            merge_args: List[str] = []
            if no_fast_forward:
                merge_args.append("--no-ff")
            if squash:
                merge_args.append("--squash")
            merge_args.append(source_branch)

            output = repo.git.merge(*merge_args)
            cleaned_output = output.strip()
            logger.info(
                "Merged branch %s into %s. Output: %s", source_branch, destination, cleaned_output
            )
            message = f"Merged {source_branch} into {destination}."
            if cleaned_output:
                message += f" Git replied: {cleaned_output}"
            return message
        except GitCommandError as error:
            stderr = (error.stderr or "").strip()
            if "CONFLICT" in stderr.upper():
                return (
                    "Merge resulted in conflicts. Please resolve them manually and commit the merge. "
                    f"Git reported: {stderr or error}"
                )
            return self._handle_tool_error("merging branches", error)
        except Exception as error:  # pragma: no cover - handled via _handle_tool_error
            return self._handle_tool_error("merging branches", error)

    @function_tool
    async def mv(self, source: str, destination: str) -> str:
        """Rename or move a tracked file."""
        try:
            repo = self._repo()
            if not source or not destination:
                return "Both source and destination paths are required to move a file."
            repo.git.mv(source, destination)
            logger.info("Renamed %s to %s", source, destination)
            return f"Moved {source} to {destination}."
        except GitCommandError as error:
            return self._handle_tool_error("moving files", error)
        except Exception as error:  # pragma: no cover - handled via _handle_tool_error
            return self._handle_tool_error("moving files", error)

    @function_tool
    async def rm(self, paths: Sequence[str], force: bool = False) -> str:
        """Remove tracked files from the working tree and index."""
        try:
            repo = self._repo()
            targets = [path.strip() for path in paths if path and path.strip()]
            if not targets:
                return "Provide at least one path to remove."
            args: List[str] = []
            if force:
                args.append("-f")
            repo.git.rm(*(args + targets))
            removed_list = ", ".join(targets)
            logger.info("Removed tracked files: %s", removed_list)
            return f"Removed the following tracked files: {removed_list}."
        except GitCommandError as error:
            return self._handle_tool_error("removing files", error)
        except Exception as error:  # pragma: no cover - handled via _handle_tool_error
            return self._handle_tool_error("removing files", error)

    @function_tool
    async def clean(self, directories: bool = False, force: bool = False) -> str:
        """Remove untracked files (and optionally directories)."""
        try:
            repo = self._repo()
            if not force:
                return (
                    "Cleaning requires force=True to avoid accidental deletions. "
                    "Re-run with force=True if you are sure."
                )
            args = ["-f"]
            if directories:
                args.append("-d")
            output = repo.git.clean(*args)
            cleaned = output.strip() or "Nothing to clean."
            logger.info("Cleaned untracked items. Git replied: %s", cleaned)
            return cleaned
        except GitCommandError as error:
            return self._handle_tool_error("cleaning untracked files", error)
        except Exception as error:  # pragma: no cover - handled via _handle_tool_error
            return self._handle_tool_error("cleaning untracked files", error)

    @function_tool
    async def check_current_branch(self) -> str:
        """Called when user wants to know the current branch in the repo."""
        try:
            repo = self._repo()
            current_branch = repo.active_branch.name
            logger.info(
                "Current branch in repo at %s is %s", repo.working_dir, current_branch
            )
            return f"Current branch in the repo is {current_branch}."
        except Exception as error:  # pragma: no cover - handled via _handle_tool_error
            return self._handle_tool_error("checking the current branch", error)

    @function_tool
    async def create_branch(self, branch_name: str) -> str:
        """Called when user wants to create a new branch in the repo for Codex to work on."""
        try:
            repo = self._repo()
            if branch_name in [head.name for head in repo.heads]:
                logger.info(
                    "Branch %s already exists in repo at %s", branch_name, repo.working_dir
                )
                return (
                    f"The branch {branch_name} already exists. "
                    "Please pick a different name or switch to it."
                )
            repo.git.checkout("HEAD", b=branch_name)
            logger.info(
                "Created and checked out new branch %s in repo at %s",
                branch_name,
                repo.working_dir,
            )
            return f"Created and checked out new branch {branch_name} in the repo."
        except Exception as error:  # pragma: no cover - handled via _handle_tool_error
            return self._handle_tool_error("creating a new branch", error)

    @function_tool
    async def commit_changes(self, commit_message: str) -> str:
        """Called when user wants to commit all current changes with a message."""
        try:
            repo = self._repo()
            if not repo.is_dirty(untracked_files=True):
                logger.info("No changes to commit in repo at %s", repo.working_dir)
                return "There are no changes to commit."

            repo.git.add(all=True)
            commit = repo.index.commit(commit_message)
            logger.info(
                "Committed changes in repo at %s with message '%s'. Commit id: %s",
                repo.working_dir,
                commit_message,
                commit.hexsha,
            )
            return f"Committed changes with message: {commit_message}."
        except Exception as error:  # pragma: no cover - handled via _handle_tool_error
            return self._handle_tool_error("committing changes", error)

    @function_tool
    async def pull_updates(
        self, remote_name: str = "origin", branch_name: Optional[str] = None
    ) -> str:
        """Called when user wants to pull the latest updates from the remote branch."""
        try:
            repo = self._repo()
            active_branch = repo.active_branch.name
            branch_to_pull = branch_name or active_branch
            remote = repo.remote(remote_name)
            pull_infos = remote.pull(branch_to_pull)
            summaries = ", ".join(
                info.summary for info in pull_infos if getattr(info, "summary", None)
            )
            logger.info(
                "Pulled updates from %s/%s in repo at %s. Summaries: %s",
                remote_name,
                branch_to_pull,
                repo.working_dir,
                summaries,
            )
            if not summaries:
                summaries = "Pull completed with no additional details."
            return f"Pulled latest updates from {remote_name}/{branch_to_pull}. {summaries}"
        except Exception as error:  # pragma: no cover - handled via _handle_tool_error
            return self._handle_tool_error("pulling updates", error)

    @function_tool
    async def fetch_updates(self, remote_name: str = "origin") -> str:
        """Called when user wants to fetch updates from the remote without merging."""
        try:
            repo = self._repo()
            remote = repo.remote(remote_name)
            fetch_infos = remote.fetch()
            summaries = ", ".join(
                info.summary for info in fetch_infos if getattr(info, "summary", None)
            )
            logger.info(
                "Fetched updates from %s in repo at %s. Summaries: %s",
                remote_name,
                repo.working_dir,
                summaries,
            )
            if not summaries:
                summaries = "Fetch completed with no additional details."
            return f"Fetched updates from {remote_name}. {summaries}"
        except Exception as error:  # pragma: no cover - handled via _handle_tool_error
            return self._handle_tool_error("fetching updates", error)

    @function_tool
    async def list_branches(self) -> str:
        """Called when user wants to list all local branches."""
        try:
            repo = self._repo()
            branches = [head.name for head in repo.heads]
            current_branch = repo.active_branch.name

            if not branches:
                logger.info("No branches found in repo at %s", repo.working_dir)
                return "No branches found in the repository."

            formatted_branches = [
                f"{name} (current)" if name == current_branch else name for name in branches
            ]
            branch_list = ", ".join(formatted_branches)
            logger.info("Listed branches in repo at %s: %s", repo.working_dir, branch_list)
            return f"The local branches are: {branch_list}."
        except Exception as error:  # pragma: no cover - handled via _handle_tool_error
            return self._handle_tool_error("listing branches", error)

    @function_tool
    async def delete_branch(self, branch_name: str, force: bool = False) -> str:
        """Called when user wants to delete a local branch."""
        try:
            repo = self._repo()
            current_branch = repo.active_branch.name
            if branch_name == current_branch:
                logger.info(
                    "Attempted to delete current branch %s in repo at %s",
                    branch_name,
                    repo.working_dir,
                )
                return "Cannot delete the branch you are currently on. Please switch to another branch first."

            if branch_name not in [head.name for head in repo.heads]:
                logger.info(
                    "Attempted to delete non-existent branch %s in repo at %s",
                    branch_name,
                    repo.working_dir,
                )
                return f"The branch {branch_name} does not exist."

            flag = "-D" if force else "-d"
            repo.git.branch(flag, branch_name)
            logger.info(
                "Deleted branch %s in repo at %s with force=%s",
                branch_name,
                repo.working_dir,
                force,
            )
            return f"Deleted branch {branch_name}."
        except Exception as error:  # pragma: no cover - handled via _handle_tool_error
            return self._handle_tool_error("deleting the branch", error)

    @function_tool
    async def push_branch(
        self,
        remote_name: str = "origin",
        branch_name: Optional[str] = None,
        set_upstream: Optional[bool] = None,
    ) -> str:
        """Called when user wants to push the current or specified branch to a remote."""
        try:
            repo = self._repo()
            remote = repo.remote(remote_name)

            active_branch = repo.active_branch
            branch_to_push = branch_name or active_branch.name

            if branch_to_push not in [head.name for head in repo.heads]:
                logger.info(
                    "Attempted to push non-existent branch %s in repo at %s",
                    branch_to_push,
                    repo.working_dir,
                )
                return f"The branch {branch_to_push} does not exist locally."

            head_ref = next(head for head in repo.heads if head.name == branch_to_push)
            tracking_branch = head_ref.tracking_branch()

            should_set_upstream = (
                set_upstream if set_upstream is not None else tracking_branch is None
            )

            if should_set_upstream:
                push_result = remote.push(f"{branch_to_push}:{branch_to_push}", set_upstream=True)
            else:
                push_result = remote.push(branch_to_push)

            summaries = ", ".join(
                info.summary for info in push_result if getattr(info, "summary", None)
            )
            logger.info(
                "Pushed branch %s to %s from repo at %s. Set upstream: %s. Summaries: %s",
                branch_to_push,
                remote_name,
                repo.working_dir,
                should_set_upstream,
                summaries,
            )
            if not summaries:
                summaries = "Push completed with no additional details."

            upstream_msg = (
                "Upstream branch configured."
                if should_set_upstream
                else "Used existing upstream."
            )
            return f"Pushed {branch_to_push} to {remote_name}. {upstream_msg} {summaries}"
        except Exception as error:  # pragma: no cover - handled via _handle_tool_error
            return self._handle_tool_error("pushing the branch", error)

    @function_tool
    async def switch_branch(self, branch_name: str) -> str:
        """Called when user wants to switch to an existing branch."""
        try:
            repo = self._repo()
            if branch_name not in [head.name for head in repo.heads]:
                logger.info(
                    "Attempted to switch to non-existent branch %s in repo at %s",
                    branch_name,
                    repo.working_dir,
                )
                return f"The branch {branch_name} does not exist."

            repo.git.checkout(branch_name)
            logger.info(
                "Switched to branch %s in repo at %s",
                branch_name,
                repo.working_dir,
            )
            return f"Switched to branch {branch_name}."
        except Exception as error:  # pragma: no cover - handled via _handle_tool_error
            return self._handle_tool_error("switching branches", error)
