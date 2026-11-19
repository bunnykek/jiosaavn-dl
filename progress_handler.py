#!/usr/bin/env python3
"""
Progress handler using rich library, matching spotdl/spotify-downloader style.

Features:
- rich.progress.Progress with transient=True
- Custom column layout matching spotdl style
- Pulse animation at 0%
- Multiple task support with global "Total" task when >4 items
- TTY detection with fallback to plain logging
"""

import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Callable
from contextlib import contextmanager

from rich.progress import (
    Progress,
    ProgressColumn,
    BarColumn,
    TimeRemainingColumn,
    Task,
    TaskID,
)
from rich.text import Text
from rich.console import Console


class DescriptionColumn(ProgressColumn):
    """Description column truncated to ~1/3 terminal width with ellipsis."""
    
    def render(self, task: Task) -> Text:
        term_width = self._get_terminal_width() or 80
        max_width = max(8, term_width // 4) if term_width < 80 else max(10, term_width // 3)
        desc = task.description or ""
        if len(desc) > max_width:
            desc = desc[:max(0, max_width - 3)] + "..." if max_width > 3 else desc[:max_width]
        return Text(desc, style="bold")
    
    def _get_terminal_width(self) -> int:
        try:
            import shutil
            return shutil.get_terminal_size().columns
        except Exception:
            return 80


class StatusColumn(ProgressColumn):
    """Fixed-width 18-char status message column, dim/grey."""
    
    def render(self, task: Task) -> Text:
        message = task.fields.get("message", "")
        term_width = self._get_terminal_width() or 80
        max_width = 18 if term_width >= 80 else max(8, min(14, term_width // 6))
        if len(message) > max_width:
            message = message[:max(0, max_width - 3)] + "..." if max_width > 3 else message[:max_width]
        else:
            message = message.ljust(max_width)
        return Text(message, style="dim")
    def _get_terminal_width(self) -> int:
        try:
            import shutil
            return shutil.get_terminal_size().columns
        except Exception:
            return 80
            

class PercentageColumn(ProgressColumn):
    """Percentage column: [progress.percentage]{task.percentage:>3.0f}% (green)."""
    
    def render(self, task: Task) -> Text:
        return Text(f"{task.percentage:>3.0f}%", style="green")


class PulseBarColumn(ProgressColumn):
    """BarColumn with pulse animation at 0% and green finished style."""
    
    def __init__(self, bar_width: Optional[int] = None):
        self.bar_width = bar_width
        super().__init__()
    
    def render(self, task: Task) -> Text:
        from rich.progress import BarColumn
        bar = BarColumn(bar_width=None, finished_style="green")
        return bar.render(task)


class DownloadProgressHandler:
    """
    Progress handler matching spotdl/spotify-downloader style using rich.
    
    Features:
    - transient=True progress bar
    - Custom columns matching spotdl style
    - Pulse animation at 0%
    - Multiple download tasks + optional total task
    - TTY detection with fallback to plain logging
    """
    
    # Lifecycle stage percentages
    STAGE_PERCENTAGES = {
        "searching": 25,
        "getting_meta": 40,
        "downloading_start": 40,
        "downloading_end": 70,
        "converting_start": 70,
        "converting_end": 95,
        "embedding_metadata": 95,
        "done": 100,
    }
    
    def __init__(
        self,
        console: Optional[Console] = None,
        simple_tui: Optional[bool] = None,
        total_items: int = 0,
        auto_total_task: bool = True,
        total_task_threshold: int = 4,
    ):
        """
        Initialize progress handler.
        
        Args:
            console: Rich Console instance (auto-created if None)
            simple_tui: If True, use simple logging instead of rich progress.
                       If None (default), auto-detect based on TTY.
            total_items: Total number of items to download
            auto_total_task: Whether to auto-create "Total" task
            total_task_threshold: Minimum items to create "Total" task
        """
        self.console = console or Console(stderr=True)
        # If simple_tui is explicitly set, use it. Otherwise auto-detect.
        self.simple_tui = simple_tui if simple_tui is not None else not self.console.is_terminal
        self.total_items = total_items
        self.auto_total_task = auto_total_task
        self.total_task_threshold = total_task_threshold
        
        # Task tracking
        self._tasks: Dict[str, TaskID] = {}
        self._total_task_id: Optional[TaskID] = None
        self._progress: Optional[Progress] = None
        self._completed_count = 0
        self._lock = threading.Lock()
        self._active = False
        
        # Progress instance (created lazily)
        self._progress_instance: Optional[Progress] = None
    
    def _create_progress(self) -> Progress:
        """Create the Progress instance with custom columns."""
        term_width = getattr(self.console, "width", 80) or 80
        columns = [DescriptionColumn()]
        if term_width >= 50:
            columns.append(StatusColumn())
        columns.extend([PulseBarColumn(bar_width=None), PercentageColumn()])
        if term_width >= 80:
            columns.append(TimeRemainingColumn())
        return Progress(
            *columns,
            console=self.console,
            transient=True,
            expand=True,
        )
    
    def __enter__(self):
        """Enter context manager."""
        if self.simple_tui:
            self._active = True
            return self
        
        progress = self._create_progress()
        self._progress_instance = progress
        self._active = True
        self._progress_instance.__enter__()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._progress_instance is not None:
            self._progress_instance.__exit__(None, None, None)
            self._progress_instance = None
        self._active = False
        return False
    
    def add_download_task(self, song_id: str, description: str) -> TaskID:
        """Add a download task for a song."""
        if self._progress_instance is None:
            raise RuntimeError("Progress not started. Use 'with handler:' context.")
        
        task_id = self._progress_instance.add_task(
            description=description,
            total=100,
            start=False,
            message="Waiting",
        )
        
        with self._lock:
            self._tasks[song_id] = task_id
        
        return task_id
    
    def _maybe_create_total_task(self):
        """Create 'Total' task if threshold met and not already created."""
        if (self.auto_total_task and 
            self._total_task_id is None and 
            len(self._tasks) >= self.total_task_threshold):
            self._total_task_id = self._progress_instance.add_task(
                description="Total",
                total=self.total_items or len(self._tasks),
                start=True,
                message="0/{} complete".format(self.total_items or len(self._tasks)),
            )
    
    def update_stage(self, song_id: str, stage: str, message: str = ""):
        """Update task to a lifecycle stage percentage."""
        task_id = self._tasks.get(song_id)
        if task_id is None:
            return
        
        progress_pct = self.STAGE_PERCENTAGES.get(stage, 0)
        message = message or stage.capitalize()
        
        if self.simple_tui:
            self._log_simple(song_id, stage, message)
        else:
            self._progress_instance.update(
                task_id,
                completed=progress_pct,
                message=message,
                advance=0,
            )
            if not self._progress_instance.tasks[task_id].started:
                self._progress_instance.start_task(task_id)
            self._progress_instance.refresh()
    
    def update_progress(self, song_id: str, progress_pct: float, message: str = ""):
        """Update task progress (0-100)."""
        task_id = self._tasks.get(song_id)
        if task_id is None:
            return
        
        progress_pct = max(0, min(100, progress_pct))
        
        if self.simple_tui:
            self._log_simple(f"{song_id}: {progress_pct:.0f}%")
        else:
            self._progress_instance.update(
                task_id,
                completed=progress_pct,
                message=message,
            )
            if not self._progress_instance.tasks[task_id].started:
                self._progress_instance.start_task(task_id)
            self._progress_instance.refresh()
    
    def complete_task(self, song_id: str, message: str = "Done"):
        """Mark task as complete."""
        task_id = self._tasks.get(song_id)
        if task_id is None:
            return
        
        if self.simple_tui:
            self._log_simple(f"Done: {song_id}")
        else:
            self._progress_instance.update(
                task_id,
                completed=100,
                message=message or "Done",
            )
            self._progress_instance.refresh()
            self._completed_count += 1
            
            # Update total task
            if self._total_task_id is not None:
                self._progress_instance.update(
                    self._total_task_id,
                    advance=1,
                    message=f"{self._completed_count}/{self.total_items or len(self._tasks)} complete",
                )
    
    def fail_task(self, song_id: str, error: str):
        """Mark task as failed."""
        task_id = self._tasks.get(song_id)
        if task_id is None:
            return
        
        if self.simple_tui:
            self._log_simple(f"Failed: {song_id} - {error}")
        else:
            self._progress_instance.update(
                task_id,
                message=f"Error: {error}",
                completed=100,
            )
            self._progress_instance.refresh()
    
    def _log_simple(self, *args):
        """Simple logging for non-TTY output."""
        print(" ".join(str(a) for a in args), flush=True)
    
    @property
    def is_active(self) -> bool:
        return self._active
    
    def get_task_id(self, song_id: str) -> Optional[TaskID]:
        return self._tasks.get(song_id)


@dataclass
class SongTracker:
    """
    Per-song tracker that maps yt-dlp/ffmpeg hooks to progress updates.
    
    Usage:
        tracker = SongTracker(handler, "song_id", "Song Title")
        tracker.searching()
        tracker.getting_meta()
        tracker.downloading(downloaded_bytes, total_bytes)  # called repeatedly
        tracker.converting(ffmpeg_progress)
        tracker.embedding_metadata()
        tracker.done()
    """
    
    def __init__(
        self,
        handler: DownloadProgressHandler,
        song_id: str,
        title: str,
        artist: str = "",
    ):
        self.handler = handler
        self.song_id = song_id
        self.title = title
        self.artist = artist
        self.description = f"{title} - {artist}" if artist else title
        self._task_id: Optional[TaskID] = None
        self._last_progress = 0
    
    def __enter__(self):
        self._task_id = self.handler.add_download_task(self.song_id, self.description)
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.handler.fail_task(self.song_id, str(exc_val))
        else:
            self.handler.complete_task(self.song_id)
    
    def searching(self):
        """Stage 1: Searching for song."""
        self.handler.update_stage(self.song_id, "searching", "Searching")
    
    def getting_meta(self):
        """Stage 2: Getting metadata."""
        self.handler.update_stage(self.song_id, "getting_meta", "Getting meta")
    
    def downloading(self, downloaded_bytes: int, total_bytes: int):
        """Download progress: 40-70% based on downloaded/total."""
        if total_bytes <= 0:
            return
        progress = 40 + (downloaded_bytes / total_bytes) * 30
        self.handler.update_progress(self.song_id, progress, "Downloading")
    
    def converting(self, ffmpeg_progress_pct: float):
        """Converting progress: 70-95% based on ffmpeg progress (0-100)."""
        progress = 70 + (ffmpeg_progress_pct / 100) * 25
        self.handler.update_progress(self.song_id, progress, "Converting")
    
    def embedding_metadata(self):
        """Embedding metadata: 95%."""
        self.handler.update_stage(self.song_id, "embedding_metadata", "Embedding metadata")
    
    def done(self, message: str = "Done"):
        """Mark complete."""
        self.handler.complete_task(self.song_id, "Done")
    
    def fail(self, error: str):
        """Mark as failed."""
        self.handler.fail_task(self.song_id, error)


# ============================================================
# DEMO
# ============================================================

def demo_fake_download():
    """Demo: Simulate downloading a file with progress updates."""
    import time
    import random
    
    songs = [
        ("song1", "Bohemian Rhapsody", "Queen"),
        ("song2", "Stairway to Heaven", "Led Zeppelin"),
        ("song3", "Hotel California", "Eagles"),
        ("song4", "Imagine", "John Lennon"),
        ("song5", "Sweet Child o' Mine", "Guns N' Roses"),
    ]
    
    def simulate_download(handler: DownloadProgressHandler, song_id: str, title: str, artist: str):
        """Simulate a full download lifecycle."""
        with SongTracker(handler, song_id, title, artist) as tracker:
            # Stage 1: Searching
            tracker.searching()
            time.sleep(0.5)
            
            # Stage 2: Getting metadata
            tracker.getting_meta()
            time.sleep(0.3)
            
            # Stage 3: Downloading (simulate yt-dlp progress hook)
            total_bytes = 5 * 1024 * 1024  # 5MB
            for downloaded in range(0, 5_000_001, 500_000):
                tracker.downloading(downloaded, 5_000_000)
                time.sleep(0.05)
            
            # Converting (simulate ffmpeg progress)
            for pct in range(0, 101, 10):
                tracker.converting(pct)
                time.sleep(0.05)
            
            # Embedding metadata
            tracker.embedding_metadata()
            time.sleep(0.2)
            
            # Done
            tracker.done()
    
    # Create handler with 5 items (will show Total task)
    # Force simple_tui=False to force rich progress bar (needed when running in non-TTY)
    handler = DownloadProgressHandler(
        total_items=5,
        simple_tui=False,  # Force rich progress bar for demo
    )
    
    songs = [
        ("song1", "Bohemian Rhapsody", "Queen"),
        ("song2", "Stairway to Heaven", "Led Zeppelin"),
        ("song3", "Hotel California", "Eagles"),
        ("song4", "Imagine", "John Lennon"),
        ("song5", "Sweet Child o' Mine", "Guns N' Roses"),
    ]
    
    with handler:
        # Pre-create all SongTracker objects (but don't enter them yet)
        trackers = []
        for song_id, title, artist in songs:
            tracker = SongTracker(handler, f"song_{songs.index((song_id, title, artist))+1}", title, artist)
            trackers.append(tracker)
        
        # Start all trackers (now that progress context is active)
        for tracker in trackers:
            tracker.__enter__()
        
        # Now run all downloads concurrently
        def run_tracker(tracker: SongTracker):
            tracker.searching()
            time.sleep(0.5)
            tracker.getting_meta()
            time.sleep(0.3)
            total_bytes = 5 * 1024 * 1024  # 5MB
            for downloaded in range(0, 5_000_001, 500_000):
                tracker.downloading(downloaded, 5_000_000)
                time.sleep(0.05)
            for pct in range(0, 101, 10):
                tracker.converting(pct)
                time.sleep(0.05)
            tracker.embedding_metadata()
            time.sleep(0.2)
            tracker.done()
        
        threads = []
        for tracker in trackers:
            t = threading.Thread(target=run_tracker, args=(tracker,), daemon=True)
            threads.append(t)
            t.start()
            time.sleep(0.1)  # Stagger starts
        
        for t in threads:
            t.join()


if __name__ == "__main__":
    print("=" * 60)
    print("Rich Progress Handler Demo - spotdl style")
    print("=" * 60)
    print()
    demo_fake_download()
    print()
    print("Demo complete!")


# Additional: Simple synchronous demo for quick testing
def demo_sync():
    """Synchronous demo showing all progress styles."""
    print("\n" + "=" * 60)
    print("Testing Progress Styles")
    print("=" * 60)
    
    # Test 1: Compact style
    print("\n1. Compact style (default):")
    with DownloadProgressHandler(total_items=3) as handler:
        for i in range(1, 4):
            handler.add_download_task(f"song{i}", f"Song {i}")
            handler.update_stage(f"song{i}", "searching")
            time.sleep(0.2)
            handler.update_stage(f"song{i}", "getting_meta")
            time.sleep(0.2)
            for p in range(0, 101, 10):
                handler.update_progress(f"song{i}", p, "Downloading")
                time.sleep(0.05)
            handler.complete_task(f"song{i}")
        time.sleep(0.5)
    
    # Test 2: Pulse style
    print("\n2. Pulse style:")
    handler2 = DownloadProgressHandler(total_items=1, simple_tui=False)
    handler2.progress_style = "pulse"
    with handler2:
        handler2.add_download_task("song1", "Test Song")
        handler.update_stage("song1", "searching")
        time.sleep(0.3)
        for p in range(0, 101, 5):
            handler.update_progress("song1", p, "Downloading")
            time.sleep(0.02)
        handler.complete_task("song1")
    
    print("\nAll demos complete!")


if __name__ == "__main__":
    demo_fake_download()
