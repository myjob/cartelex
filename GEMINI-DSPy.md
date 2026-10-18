🎯 Role & Objective
Your goal is to build highly modular, declarative AI pipelines. You prioritize architectural integrity, type safety, and strict adherence to the local development environment.

🏗️ DSPy Core Principles
Rule 1: Declarative Signatures Only. Strictly no manual prompt engineering or f-string prompt templates. All task definitions must be encapsulated in a dspy.Signature. Fields must use clear desc="..." strings to guide the model.

Rule 2: Logic-Free Signatures. Keep dspy.Signature classes purely declarative. All business logic, branching, and retrieval orchestration (SQL lookups, Vector searches) must reside within the forward method of a dspy.Module.

Rule 3: Structured Outputs. Always use Pydantic models (via dspy.OutputField) for extraction tasks. This ensures type safety and seamless integration with the Antigravity backend.

🛠️ Environment & Pathing Rules
Rule 4: Venv Primacy. You must exclusively use the virtual environment located at .venv . When generating shell commands or setup scripts, ensure they target this specific path.

Rule 5: Test Localization. All test code must be placed in the ./tests subfolder relative to the specific source folder being worked on (e.g., src/pipelines/tests/).

Rule 6: use always the .venv in the root folder of this project for python execution

Rule 7: use always unittest for testing

📝 Design Patterns to Follow
The "Provisioner" Pattern: Database clients and Vector stores are injected during __init__.

The "Forward" Dispatch: The forward method handles the "Slick Pipe" (e.g., SQL -> Fallback -> Vector -> DSPy Extraction).

Functional Composition: Prefer small, reusable dspy.Module components over monolithic agents.


Self-Correction & Code Review Checklist
Before delivering any code, you must internally verify your output against this checklist. If any point fails, you must rewrite the code.

[ ] Signature Check: Is there a single f-string being used as a prompt? (If yes, replace with dspy.Signature).

[ ] Module Separation: Is the SQL/Vector retrieval logic outside the forward method? (If yes, move it into forward).

[ ] Type Safety: Does the OutputField reference a Pydantic BaseModel?

[ ] Pathing: Does the code assume a global python environment? (If yes, update to reference .venv).

[ ] Conflict Awareness: Have I checked if the suggested imports overlap with existing modules in /src?

[ ] Test Coverage: Have I provided a test file in the correct ./tests subdirectory?