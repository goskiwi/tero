"""The explicit model-facing tool surface. No XML parsing or legacy aliases."""

from pydantic import BaseModel, ConfigDict, Field


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ListArgs(Args):
    path: str = "."


class ReadArgs(Args):
    path: str
    start: int = Field(default=1, ge=1)
    end: int = Field(default=200, ge=1)


class SearchArgs(Args):
    pattern: str = Field(min_length=1)
    path: str = "."


class EditArgs(Args):
    path: str
    old_text: str = Field(min_length=1)
    new_text: str


class WriteArgs(Args):
    path: str
    content: str


class ShellArgs(Args):
    command: str = Field(min_length=1)
    timeout: int = Field(default=60, ge=1)


class DelegateArgs(Args):
    task: str = Field(min_length=1)
    max_turns: int = Field(default=4, ge=1, le=8)


TOOLS = {
    "list_files": (ListArgs, "List a workspace directory (up to 200 entries)."),
    "read_file": (
        ReadArgs,
        "Read a UTF-8 file range. Read before every first edit; reread after conflicts.",
    ),
    "search": (SearchArgs, "Search literal text in workspace files; output is bounded."),
    "edit_file": (
        EditArgs,
        "Replace one exact unique block in a previously read file. Preserve other bytes.",
    ),
    "write_file": (
        WriteArgs,
        "Create a file, or replace an existing file only after reading its current version.",
    ),
    "run_shell": (
        ShellArgs,
        "Run an approved shell command in the workspace. Not a sandbox; may have external effects.",
    ),
    "delegate": (
        DelegateArgs,
        "Delegate bounded read-only analysis. Child cannot modify files or delegate again.",
    ),
}
READ_TOOLS = frozenset({"list_files", "read_file", "search"})


def tool_schemas(mode, *, child=False):
    names = READ_TOOLS if mode == "ask" else TOOLS
    result = []
    for name, (args, description) in TOOLS.items():
        if name not in names or (child and name == "delegate"):
            continue
        schema = args.model_json_schema()
        schema.pop("title", None)
        # Responses strict mode requires all properties; defaults still support local callers.
        schema["required"] = list(schema["properties"])
        result.append(
            {
                "type": "function",
                "name": name,
                "description": description,
                "parameters": schema,
                "strict": True,
            }
        )
    return result
