"""
cli.py
------
The ``axio`` command-line interface — the same operations the MCP server exposes, for humans
and for shell scripts. Both surfaces are thin wrappers over the same engine, so they cannot
diverge.

Commands::

    axio doctor      Diagnose the environment
    axio inspect     Parse and display Zeiss XML metadata
    axio estimate    Size a job before running it (canvas, RAM, disk, time)
    axio validate    Check prerequisites without running
    axio stitch      Run the full stitching pipeline
    axio qc          Measure a stitched mosaic
    axio outputs     List stitched outputs in a directory
    axio serve       Run the MCP server over stdio
    axio version     Print version and dependency status
    axio agent ...   Wire the pipeline into Claude Code / Codex / Antigravity / ...

Every command accepts ``--json`` for machine-readable output.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text

from . import __version__
from .models import ProgressEvent, StitchConfig

app = typer.Typer(
    name="axio",
    help="[bold cyan]AXIO Stitching Studio[/bold cyan] - high-throughput Zeiss microscopy tile stitching.",
    rich_markup_mode="rich",
    no_args_is_help=True,
)
agent_app = typer.Typer(
    name="agent",
    help="Wire AXIO into AI agent platforms (Claude Code, Codex/ChatGPT, Antigravity, ...).",
    no_args_is_help=True,
)
app.add_typer(agent_app, name="agent")

console = Console(stderr=False)
err_console = Console(stderr=True)


def _resolve_source(source: "Path | None", xml: "Path | None") -> Path:
    """Accept --source (any format) or the legacy --xml alias; exactly one is required."""
    chosen = source or xml
    if chosen is None:
        err_console.print("[bold red]x[/bold red] provide --source (a Zeiss XML, Fiji "
                          "TileConfiguration, OME-TIFF, positions .json, or a tile directory)")
        raise typer.Exit(2)
    return chosen.resolve()


_STATUS_STYLE = {
    "ok": "[green]OK[/green]",
    "warn": "[yellow]WARN[/yellow]",
    "fail": "[red]FAIL[/red]",
    "installed": "[green]installed[/green]",
    "absent": "[dim]absent[/dim]",
    "drifted": "[yellow]drifted[/yellow]",
    "foreign": "[red]foreign[/red]",
}


def _emit(payload: dict, json_output: bool) -> bool:
    """
    Print ``payload`` as JSON and return True when that was the requested format.

    Deliberately ``sys.stdout`` rather than the Rich console: Rich soft-wraps at the terminal
    width and would insert newlines inside long strings, corrupting exactly the Windows paths
    that ``--json`` exists to hand to another program.
    """
    if json_output:
        sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")
        return True
    return False


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

@app.command()
def doctor(
    out_dir: Optional[Path] = typer.Option(None, "--out-dir", help="Output directory to check for space and writability"),
    json_output: bool = typer.Option(False, "--json", help="Output JSON"),
) -> None:
    """Diagnose the environment and report exactly what is missing."""
    from .doctor import run_doctor

    report = run_doctor(out_dir)
    if _emit(report.to_dict(), json_output):
        raise typer.Exit(0 if report.ok else 1)

    table = Table(title=f"AXIO Stitching Studio v{__version__} - environment", border_style="cyan")
    table.add_column("Check", style="bold")
    table.add_column("Status", justify="center")
    table.add_column("Detail")
    for check in report.checks:
        table.add_row(check.name, _STATUS_STYLE.get(check.status, check.status), check.detail)
    console.print("\n")
    console.print(table)

    actionable = [c for c in report.checks if c.status != "ok" and c.fix]
    if actionable:
        console.print("\n[bold]To fix:[/bold]")
        for check in actionable:
            marker = "[red]x[/red]" if check.status == "fail" else "[yellow]![/yellow]"
            console.print(f"  {marker} {check.name}: [dim]{check.fix}[/dim]")

    console.print(f"\n[bold]{report.summary()}[/bold]")
    raise typer.Exit(0 if report.ok else 1)


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------

@app.command()
def inspect(
    source: Optional[Path] = typer.Option(None, "--source", "-s", show_default=False,
        help="Any dataset: Zeiss XML, Fiji TileConfiguration.txt, OME-TIFF, positions .json, or a tile directory"),
    xml: Optional[Path] = typer.Option(None, "--xml", help="Alias for --source (Zeiss XML)", show_default=False),
    json_output: bool = typer.Option(False, "--json", help="Output JSON"),
) -> None:
    """Parse and display Zeiss XML metadata (scenes, tiles, channels, Z)."""
    from .engine import StitchingEngine

    try:
        config = StitchConfig(source=_resolve_source(source, xml), out_dir=Path.cwd())
        metadata = StitchingEngine(config).inspect_metadata()
    except Exception as exc:
        if _emit({"error": str(exc)}, json_output):
            raise typer.Exit(1)
        err_console.print(f"[bold red]x Error:[/bold red] {exc}")
        raise typer.Exit(1)

    if _emit(metadata, json_output):
        return

    table = Table(title=f"[bold]Zeiss XML metadata[/bold]  [dim]{xml.name}[/dim]", border_style="cyan")
    table.add_column("Scene", style="bold cyan", justify="right")
    table.add_column("Tiles", justify="right")
    table.add_column("Sample file", style="dim")
    for scene in metadata.get("scenes", []):
        tiles = scene.get("tiles", [])
        table.add_row(str(scene["scene_id"]), str(len(tiles)), tiles[0]["filename"] if tiles else "-")
    console.print("\n")
    console.print(table)
    console.print(
        f"\n[dim]XML type:[/dim] {metadata.get('xml_type')}   "
        f"[dim]Total scenes:[/dim] {metadata.get('total_scenes')}   "
        f"[dim]Total tiles:[/dim] {metadata.get('total_tiles')}"
    )
    if metadata.get("pixel_scale_um"):
        console.print(f"[dim]Pixel scale:[/dim] {metadata['pixel_scale_um']:.4f} um/px")


# ---------------------------------------------------------------------------
# estimate
# ---------------------------------------------------------------------------

@app.command()
def estimate(
    source: Optional[Path] = typer.Option(None, "--source", "-s", show_default=False,
        help="Any dataset: Zeiss XML, Fiji TileConfiguration.txt, OME-TIFF, positions .json, or a tile directory"),
    xml: Optional[Path] = typer.Option(None, "--xml", help="Alias for --source (Zeiss XML)", show_default=False),
    out_dir: Path = typer.Option(..., "--out-dir", help="Intended output directory", show_default=False),
    correction: str = typer.Option("basicpy", "--correction", help="[basicpy|median|spatial|none]"),
    algorithm: str = typer.Option("phase", "--algorithm", help="[phase|sift|coordinate]"),
    scene: Optional[int] = typer.Option(None, "--scene", help="Single scene index (0-based)"),
    ref_tag: str = typer.Option("", "--ref-tag", help="Split-channel reference tag"),
    target_tags: str = typer.Option("", "--target-tags", help="Comma-separated target channel tags"),
    z_mode: str = typer.Option("none", "--z-mode", help="[none|mip_align_3d|ref_slice_3d|mip_output_only]"),
    overlap: float = typer.Option(0.1, "--overlap", help="Tile overlap fraction for a filename-grid folder"),
    grid_cols: Optional[int] = typer.Option(None, "--grid-cols", help="Columns, for filenames with a linear position index"),
    pixel_size_um: Optional[float] = typer.Option(None, "--pixel-size-um", help="Micrometres per pixel"),
    json_output: bool = typer.Option(False, "--json", help="Output JSON"),
) -> None:
    """Size a stitching job before running it: canvas, peak RAM, disk, rough time."""
    from .estimate import estimate_stitch

    try:
        config = StitchConfig(
            source=_resolve_source(source, xml),
            out_dir=out_dir.resolve(),
            correction=correction,
            algorithm=algorithm,
            scene=scene,
            ref_tag=ref_tag,
            target_tags=[t.strip() for t in target_tags.split(",") if t.strip()],
            z_mode=z_mode,
            overlap=overlap,
            grid_cols=grid_cols,
            pixel_size_um=pixel_size_um,
        )
        result = estimate_stitch(config)
    except Exception as exc:
        if _emit({"error": str(exc)}, json_output):
            raise typer.Exit(1)
        err_console.print(f"[bold red]x Error:[/bold red] {exc}")
        raise typer.Exit(1)

    payload = result.to_dict()
    if _emit(payload, json_output):
        raise typer.Exit(0 if result.verdict != "will_not_fit" else 1)

    table = Table(title="Stitching estimate", border_style="cyan")
    table.add_column("Scene", justify="right", style="bold cyan")
    table.add_column("Tiles", justify="right")
    table.add_column("Canvas", justify="right")
    table.add_column("Frames", justify="right")
    table.add_column("Output", justify="right")
    table.add_column("Peak RAM", justify="right")
    table.add_column("Time", justify="right")
    for scene_data in payload["scenes"]:
        table.add_row(
            str(scene_data["scene_id"]),
            str(scene_data["tiles"]),
            f"{scene_data['canvas_width']}x{scene_data['canvas_height']}",
            str(scene_data["output_frames"]),
            scene_data["output_size"],
            scene_data["peak_ram"],
            scene_data["estimated_time"],
        )
    console.print("\n")
    console.print(table)

    totals, machine = payload["totals"], payload["machine"]
    console.print(
        f"\n[dim]Peak RAM:[/dim] {totals['peak_ram']} of {machine['ram_available']} available   "
        f"[dim]Disk needed:[/dim] {totals['disk_needed']} of {machine['disk_free']} free   "
        f"[dim]Time:[/dim] ~{totals['estimated_time']} [dim](order-of-magnitude)[/dim]"
    )

    verdict_style = {"ok": "bold green", "tight": "bold yellow", "will_not_fit": "bold red"}
    console.print(f"\n[{verdict_style[result.verdict]}]Verdict: {result.verdict}[/{verdict_style[result.verdict]}]")
    for reason in payload["reasons"]:
        console.print(f"  - {reason}")
    if payload["advice"]:
        console.print("\n[bold]To make it fit:[/bold]")
        for advice in payload["advice"]:
            console.print(f"  - {advice}")
    for warning in payload["warnings"]:
        console.print(f"  [yellow]![/yellow] {warning}")

    raise typer.Exit(0 if result.verdict != "will_not_fit" else 1)


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

@app.command()
def validate(
    source: Optional[Path] = typer.Option(None, "--source", "-s", show_default=False,
        help="Any dataset: Zeiss XML, Fiji TileConfiguration.txt, OME-TIFF, positions .json, or a tile directory"),
    xml: Optional[Path] = typer.Option(None, "--xml", help="Alias for --source (Zeiss XML)", show_default=False),
    out_dir: Path = typer.Option(Path("./output"), "--out-dir", help="Output directory to check"),
    correction: str = typer.Option("basicpy", "--correction"),
    algorithm: str = typer.Option("phase", "--algorithm"),
    scene: Optional[int] = typer.Option(None, "--scene"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Validate a stitching configuration without running the pipeline."""
    from .engine import StitchingEngine

    try:
        config = StitchConfig(
            source=_resolve_source(source, xml),
            out_dir=out_dir.resolve(),
            correction=correction,
            algorithm=algorithm,
            scene=scene,
        )
        result = StitchingEngine(config).validate_config()
    except Exception as exc:
        if _emit({"valid": False, "errors": [str(exc)], "warnings": []}, json_output):
            raise typer.Exit(1)
        err_console.print(f"[bold red]x Validation error:[/bold red] {exc}")
        raise typer.Exit(1)

    if _emit(result, json_output):
        raise typer.Exit(0 if result["valid"] else 1)

    if result["valid"]:
        console.print("[bold green]OK Configuration is valid[/bold green]")
    else:
        console.print("[bold red]x Configuration has errors[/bold red]")
    for message in result.get("errors", []):
        console.print(f"  [red]x[/red] {message}")
    for message in result.get("warnings", []):
        console.print(f"  [yellow]![/yellow] {message}")
    if not result["valid"]:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# stitch
# ---------------------------------------------------------------------------

@app.command()
def stitch(
    source: Optional[Path] = typer.Option(None, "--source", "-s", show_default=False,
        help="Any dataset: Zeiss XML, Fiji TileConfiguration.txt, OME-TIFF, positions .json, or a tile directory"),
    xml: Optional[Path] = typer.Option(None, "--xml", help="Alias for --source (Zeiss XML)", show_default=False),
    out_dir: Path = typer.Option(..., "--out-dir", help="Output directory", show_default=False),
    correction: str = typer.Option("basicpy", "--correction", help="[basicpy|median|spatial|none]"),
    algorithm: str = typer.Option("phase", "--algorithm", help="[phase|sift|coordinate]"),
    scene: Optional[int] = typer.Option(None, "--scene", help="Single scene index (0-based). Default: all."),
    ref_channel: int = typer.Option(0, "--ref-channel", help="Reference channel index"),
    ref_tag: str = typer.Option("", "--ref-tag", help="Reference tag for split-channel TIFFs"),
    target_tags: str = typer.Option("", "--target-tags", help="Comma-separated target channel tags"),
    alignment_mode: str = typer.Option("reference", "--alignment-mode", help="[reference|average|max_projection]"),
    z_mode: str = typer.Option("none", "--z-mode", help="[none|mip_align_3d|ref_slice_3d|mip_output_only]"),
    ref_z_slice: int = typer.Option(0, "--ref-z-slice", help="Reference Z-slice index"),
    overlap: float = typer.Option(0.1, "--overlap", help="Tile overlap fraction for a filename-grid folder"),
    grid_cols: Optional[int] = typer.Option(None, "--grid-cols", help="Columns, for filenames with a linear position index"),
    pixel_size_um: Optional[float] = typer.Option(None, "--pixel-size-um", help="Micrometres per pixel (converts stage-unit positions)"),
    json_output: bool = typer.Option(False, "--json", help="Output JSON instead of rich formatting"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress progress UI"),
) -> None:
    """Run the full AXIO stitching pipeline on any supported dataset (Zeiss or vendor-neutral)."""
    from .engine import StitchingEngine

    try:
        config = StitchConfig(
            source=_resolve_source(source, xml),
            out_dir=out_dir.resolve(),
            correction=correction,
            algorithm=algorithm,
            scene=scene,
            ref_channel=ref_channel,
            ref_tag=ref_tag,
            target_tags=[t.strip() for t in target_tags.split(",") if t.strip()],
            alignment_mode=alignment_mode,
            z_mode=z_mode,
            ref_z_slice=ref_z_slice,
            overlap=overlap,
            grid_cols=grid_cols,
            pixel_size_um=pixel_size_um,
        )
    except Exception as exc:
        if _emit({"success": False, "error_message": str(exc)}, json_output):
            raise typer.Exit(1)
        err_console.print(f"[bold red]x Configuration error:[/bold red] {exc}")
        raise typer.Exit(1)

    if json_output or quiet:
        result = StitchingEngine(config, progress_callback=lambda _e: None).run()
        if _emit(result.to_dict(), json_output):
            raise typer.Exit(0 if result.success else 1)
        if not result.success:
            err_console.print(f"[bold red]x Failed:[/bold red] {result.error_message}")
            raise typer.Exit(1)
        return

    status_text = Text("Initialising...", style="bold yellow")
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=40),
        TaskProgressColumn(),
        TimeElapsedColumn(),
    )
    task = progress.add_task("Stitching", total=100)

    def progress_callback(event: ProgressEvent) -> None:
        status_text.plain = event.status_message
        progress.update(task, completed=event.percent, description=event.stage.value.capitalize())

    panel = Panel(
        progress,
        title=f"[bold cyan]AXIO Stitching[/bold cyan]  [dim]{xml.name}[/dim]",
        border_style="blue",
    )
    with Live(panel, console=console, refresh_per_second=8):
        result = StitchingEngine(config, progress_callback=progress_callback).run()

    if not result.success:
        err_console.print(f"\n[bold red]x Stitching failed:[/bold red] {result.error_message}")
        raise typer.Exit(1)

    table = Table(title="Stitching complete", border_style="green", show_header=False)
    table.add_column("Key", style="bold")
    table.add_column("Value")
    table.add_row("Scenes processed", str(result.scenes_processed))
    table.add_row("Tiles processed", str(result.tiles_processed))
    table.add_row("Duration", f"{result.duration_seconds:.1f}s")
    table.add_row("Output files", str(len(result.output_paths)))
    for path in result.output_paths:
        table.add_row("  ->", str(path))
    console.print("\n")
    console.print(table)
    console.print("\n[dim]Check the result before trusting it:[/dim] axio qc <output.tif>")


# ---------------------------------------------------------------------------
# qc / outputs
# ---------------------------------------------------------------------------

@app.command()
def qc(
    path: Path = typer.Argument(..., help="Stitched .tif to measure"),
    frame: Optional[int] = typer.Option(None, "--frame", help="Page index for a multi-channel / Z-stack file"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Measure a stitched mosaic: empty area, clipping, dynamic range, seam prominence."""
    from .qc import qc_report

    report = qc_report(path, frame=frame)
    payload = report.to_dict()
    if _emit(payload, json_output):
        raise typer.Exit(0 if report.ok else 1)

    if not report.ok:
        err_console.print(f"[bold red]x QC failed:[/bold red] {report.error}")
        raise typer.Exit(1)

    metrics = payload["metrics"]
    table = Table(title=f"QC - {path.name}", border_style="cyan", show_header=False)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("Frame", f"{payload['frame_index']} of {payload['axes'] or '?'} {payload['shape']}")
    table.add_row("Size", f"{payload['width']} x {payload['height']} ({payload['dtype']}, read {payload['method']})")
    table.add_row("Mean / std", f"{metrics['mean']} / {metrics['std']}")
    table.add_row("Range (p1..p99)", f"{metrics['percentiles']['p1']:.0f} .. {metrics['percentiles']['p99']:.0f}")
    table.add_row("Empty fraction", f"{metrics['empty_fraction']:.2%}")
    table.add_row("Saturated fraction", f"{metrics['saturated_fraction']:.4%}")
    table.add_row("Seam prominence x / y", f"{metrics['seam_prominence_x']} / {metrics['seam_prominence_y']}")
    console.print("\n")
    console.print(table)

    if report.findings:
        console.print("\n[bold]Findings:[/bold]")
        for finding in report.findings:
            console.print(f"  [yellow]![/yellow] {finding}")
    else:
        console.print("\n[green]Nothing stood out.[/green]")


@app.command()
def outputs(
    directory: Path = typer.Argument(..., help="Output directory to scan"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List the stitched outputs a previous run left in a directory."""
    from .doctor import human_bytes
    from .qc import list_outputs

    found = list_outputs(directory)
    if _emit({"directory": str(directory), "outputs": found}, json_output):
        return

    if not found:
        console.print(f"[dim]No stitched_*.tif files in {directory}[/dim]")
        return

    table = Table(title=f"Stitched outputs in {directory}", border_style="cyan")
    table.add_column("File", style="bold")
    table.add_column("Shape")
    table.add_column("Size", justify="right")
    table.add_column("Preview", justify="center")
    for entry in found:
        table.add_row(
            entry["name"],
            f"{entry.get('axes') or '?'} {entry.get('shape') or ''}",
            human_bytes(entry["size_bytes"]),
            "[green]yes[/green]" if entry["preview_path"] else "[dim]no[/dim]",
        )
    console.print("\n")
    console.print(table)


# ---------------------------------------------------------------------------
# serve / version
# ---------------------------------------------------------------------------

@app.command()
def serve() -> None:
    """Run the MCP server over stdio (the same thing agent platforms launch)."""
    from .mcp_server import main as serve_main

    serve_main()


@app.command()
def version(json_output: bool = typer.Option(False, "--json")) -> None:
    """Print version and dependency status."""
    from .doctor import OPTIONAL_PACKAGES, REQUIRED_PACKAGES, _module_version

    deps = {
        pip_name: (_module_version(import_name) or "not installed")
        for import_name, pip_name, _ in (*REQUIRED_PACKAGES, *OPTIONAL_PACKAGES)
    }
    info = {
        "axio_stitching": __version__,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "dependencies": deps,
    }
    if _emit(info, json_output):
        return

    table = Table(title=f"AXIO Stitching Studio v{__version__}", border_style="cyan")
    table.add_column("Package")
    table.add_column("Status")
    for package, status_text in deps.items():
        icon = "[green]OK[/green]" if status_text != "not installed" else "[red]--[/red]"
        table.add_row(package, f"{icon} {status_text}")
    console.print("\n")
    console.print(table)
    console.print(f"\n[dim]Python:[/dim] {info['python']}")


# ---------------------------------------------------------------------------
# agent sub-app
# ---------------------------------------------------------------------------

def _agent_targets(target: str | None) -> list[str] | None:
    from .agent_integration import AGENT_TARGETS

    if not target or target == "all":
        return None
    chosen = [t.strip() for t in target.split(",") if t.strip()]
    unknown = [t for t in chosen if t not in AGENT_TARGETS]
    if unknown:
        err_console.print(
            f"[bold red]x Unknown target(s):[/bold red] {', '.join(unknown)}\n"
            f"  Known: {', '.join(AGENT_TARGETS)}"
        )
        raise typer.Exit(2)
    return chosen


@agent_app.command("status")
def agent_status(
    target: Optional[str] = typer.Option(None, "--target", help="Comma-separated targets, or 'all'"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show which agent platforms are present, and what AXIO has installed into each."""
    from .agent_integration import AGENT_TARGETS
    from .agent_runner import make_ctx, status

    ctx = make_ctx()
    chosen = _agent_targets(target) or list(AGENT_TARGETS)
    reports = [status(ctx, t) for t in chosen]

    if _emit({"targets": [r.to_dict() for r in reports]}, json_output):
        return

    table = Table(title="AXIO agent integration", border_style="cyan")
    table.add_column("Target", style="bold")
    table.add_column("Platform")
    table.add_column("Detected", justify="center")
    table.add_column("State", justify="center")
    table.add_column("MCP name", style="dim")
    for report in reports:
        table.add_row(
            report.target,
            report.label,
            "[green]yes[/green]" if report.detected else "[dim]no[/dim]",
            _STATUS_STYLE.get(report.state, report.state),
            report.mcp_name,
        )
    console.print("\n")
    console.print(table)

    for report in reports:
        details = [u for u in report.units if u.state != "absent"] + [
            k for k in report.keys if k.state != "absent"
        ]
        noteworthy = [d for d in details if d.state != "installed"]
        if not noteworthy:
            continue
        console.print(f"\n[bold]{report.target}[/bold]")
        for item in noteworthy:
            location = getattr(item, "dir", None) or getattr(item, "file", "")
            console.print(f"  {_STATUS_STYLE.get(item.state, item.state)} {location}")
            if item.detail:
                console.print(f"      [dim]{item.detail}[/dim]")


@agent_app.command("install")
def agent_install(
    target: Optional[str] = typer.Option(
        None, "--target",
        help="Comma-separated targets (claude-code, codex, antigravity, claude-desktop, gemini-cli), or 'all'",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show every file and key that would change"),
    force: bool = typer.Option(False, "--force", help="Overwrite a directory that exists but is not ours"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """
    Install the AXIO skill and register the MCP server into AI agent platforms.

    With no --target, every platform detected on this machine is configured and the rest are
    skipped - a config directory is never created for an app you do not have.
    """
    from .agent_runner import install_all, make_ctx

    ctx = make_ctx()
    chosen = _agent_targets(target)
    results = install_all(ctx, targets=chosen, dry_run=dry_run, force=force)

    if _emit({"results": [r.to_dict() for r in results]}, json_output):
        raise typer.Exit(0 if all(r.ok for r in results) else 1)

    console.print(f"\n[bold cyan]AXIO agent integration{' (dry run)' if dry_run else ''}[/bold cyan]\n")
    for result in results:
        if result.skipped:
            console.print(f"  [dim]-- {result.target}: {'; '.join(result.skipped)}[/dim]")
            continue
        if not result.ok:
            console.print(f"  [red]x[/red] [bold]{result.target}[/bold]: {result.error}")
            continue
        verb = "would install" if dry_run else ("installed" if result.changed else "already up to date")
        console.print(f"  [green]OK[/green] [bold]{result.target}[/bold]: {verb}")
        for path in result.written:
            console.print(f"      [dim]{path}[/dim]")
        for key in result.keys:
            console.print(f"      [dim]{key}[/dim]")
        for backup in result.backups:
            console.print(f"      [yellow]backup:[/yellow] [dim]{backup}[/dim]")
        for note in result.notes:
            console.print(f"      [dim]{note}[/dim]")

    if not dry_run and any(r.changed for r in results):
        console.print(
            "\n[dim]Restart the agent app to pick up the new MCP server. "
            "Verify with:[/dim] axio agent status"
        )
    raise typer.Exit(0 if all(r.ok for r in results) else 1)


@agent_app.command("uninstall")
def agent_uninstall(
    target: Optional[str] = typer.Option(None, "--target", help="Comma-separated targets, or 'all'"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be removed"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """
    Remove what AXIO installed - and only that.

    Files and config keys are removed while they still hash to what was written. Anything you
    have since edited is kept and reported.
    """
    from .agent_runner import make_ctx, uninstall_all

    results = uninstall_all(make_ctx(), targets=_agent_targets(target), dry_run=dry_run)

    if _emit({"results": [r.to_dict() for r in results]}, json_output):
        raise typer.Exit(0 if all(r.ok for r in results) else 1)

    console.print(f"\n[bold cyan]AXIO agent uninstall{' (dry run)' if dry_run else ''}[/bold cyan]\n")
    for result in results:
        if not result.removed and not result.kept and result.ok:
            console.print(f"  [dim]-- {result.target}: nothing installed[/dim]")
            continue
        icon = "[green]OK[/green]" if result.ok else "[red]x[/red]"
        console.print(f"  {icon} [bold]{result.target}[/bold]")
        for path in result.removed:
            console.print(f"      [dim]removed {path}[/dim]")
        for path in result.kept:
            console.print(f"      [yellow]kept[/yellow] [dim]{path}[/dim]")
        if result.error:
            console.print(f"      [red]{result.error}[/red]")
    raise typer.Exit(0 if all(r.ok for r in results) else 1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app()


if __name__ == "__main__":
    main()
