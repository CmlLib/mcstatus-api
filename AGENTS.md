# Code style

Prioritize readable code over short code.

## Comments and documentation

- Write comments and Swagger/XML documentation in English.
- Explain intent, reasoning, and constraints that are not obvious from the code.
- Avoid unnecessary comments that merely repeat what the code does.

## Indentation and line breaks

- Use consistent indentation that follows the conventions of the language and project.
- Do not compress multiple statements onto one line.
- Put each method call in a method chain on its own line.
- When a method call has four or more arguments, put each argument on its own line. Calls with three or fewer arguments may also be split across lines when their length harms readability.
- Do not put the body of a control statement such as `try`, `catch`, `if`, `else`, or `for` on the same line as the statement.
- Where the language permits it, braces around an `if` body may be omitted. Even without braces, the body must be indented on the next line.

```csharp
// Allowed
if (v)
    return 1;

// Allowed
if (v)
{
    return 1;
}

// Not allowed
if (v) return 1;
```

## Blank lines

- Separate different concerns or processing stages with blank lines.
- Keep related code together; do not add unnecessary blank lines between every line.

## Conditional branches

- Prefer explicit `if` / `else` statements over ternary operators.
- If conditional logic makes the flow difficult to follow, extract it into a method whose name conveys its intent.
- Do not use nested ternary operators.

# Commit and push scope

A request to commit and push this repository or a named branch includes all submodules recursively, unless the user explicitly limits the scope. In particular, a request to commit and push `snowfrost-web` includes every submodule, including `snowfrost-pages`.

- Inspect pending changes and unpushed commits in the parent repository and every submodule.
- Commit and push submodule changes to their appropriate remote branches first, starting with the deepest nested submodules.
- Commit the updated submodule pointers in the parent repository, along with the other changes in scope, and push the parent branch.
- If a submodule is in detached HEAD state, identify its intended branch before committing and pushing; do not leave new commits only on a detached HEAD.
- For `src/snowfrost-pages`, commit and push to `origin/main`. The parent `snowfrost-web` branch must point to the resulting commit on that branch.
- Verify that all resulting commits are present on their remotes and report any remaining uncommitted or unpushed changes. Submodules without changes do not need empty commits.
