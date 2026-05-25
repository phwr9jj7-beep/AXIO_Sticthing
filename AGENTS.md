# Agentic Trigger Rules

The following rules dictate when global agent workflows should be triggered in this project.

- **`/lab-commit`**: Always use this workflow when committing daily experiment updates, data processing scripts, or stitching results.
- **`/lab-reorganize`**: Triggered when moving, renaming, or archiving file paths (especially large image folders) to preserve version history properly.
- **`/wiki-ingest`**: Automatically trigger when summary reports, executive PDFs, or structured metadata are added, ensuring the knowledge base remains up-to-date.
- **`/wiki-research`**: Triggered when the AI detects gaps in theoretical knowledge regarding the current experimental conditions or imaging methodologies, maintaining strict grounding.
- **`/science-project-onboarding`**: Use for onboarding into inherited structures (this project has completed onboarding).
- **`/wiki-update`**: Trigger weekly for knowledge graph maintenance and resolution of orphan pages.
- **`/wiki-query`**: Use whenever answering biological or technical questions about the project history.
- **`/wiki-build`**: Run when synthesizing multi-page reports or manuscripts from the established knowledge base.
- **`/visualize-data`**: Trigger when the user requests generating plots, architecture diagrams of pipelines, or technical schemas.
- **`/qa-system-audit`**: Run for periodic system health checks.
- **`/project-organize`**: Triggered when the project structure begins to clutter with unsorted files in the root.
- **`/manuscript-write`**: Link to trigger generation of comprehensive papers encompassing the gathered microscopy results.
- **`/research-discovery`**: Trigger to brainstorm analysis directions for the raw stitched images before running super_scientist.

## Workspace Hygiene
Agents MUST NOT generate one-off or ad-hoc diagnostic scripts in the root project directory. They must strictly isolate all scratch work to `~/.gemini/antigravity/brain/<id>/scratch/`.
