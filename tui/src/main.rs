//! coord-tui — TUI binary.
//!
//! Thin shim: wires [`coord_tui::CoordApp`] to a custom ratatui event loop.
//! All app logic lives in `CoordApp`; this runner owns terminal setup/teardown,
//! the crossterm event loop, and ratatui rasterisers.
//!
//! ## Why not `quadraui::tui::run`?
//!
//! `quadraui::tui::run` only calls `AppLogic::handle` when `wait_events`
//! returns at least one event.  With no user input the inner loop blocks up to
//! 16 ms per iteration, returns an empty `Vec`, and skips the `for` body
//! entirely — so `CoordApp::handle`'s 5-second refresh timer never fires and
//! the dashboard freezes.  The upstream fix is `AppLogic::tick` (tracked in
//! quadraui#236).  Until that lands, the workaround here is to inject a
//! synthetic `UiEvent::WindowFocused` whenever the event queue is empty,
//! which causes `handle` to be called and its timer check to run.

use coord_tui::CoordApp;
use quadraui::{tui::TuiBackend, AppLogic, Backend, Reaction, UiEvent, Viewport};
use ratatui::{
    backend::CrosstermBackend,
    crossterm::{
        event::{
            DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture,
        },
        execute,
        terminal::{
            disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
        },
    },
    Terminal,
};
use std::{io, time::Duration};

/// Crossterm poll timeout — mirrors the constant in `quadraui::tui::run`.
const POLL_TIMEOUT: Duration = Duration::from_millis(16);

fn main() -> io::Result<()> {
    // ── Terminal setup ────────────────────────────────────────────────────────
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(
        stdout,
        EnterAlternateScreen,
        EnableMouseCapture,
        EnableBracketedPaste,
    )?;

    let crossterm_backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(crossterm_backend)?;
    terminal.clear()?;

    let mut backend = TuiBackend::new();
    let mut app = CoordApp::new();

    // Run inside catch_unwind so a panic in app code doesn't leave the
    // terminal broken.
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        run_with_tick(&mut terminal, &mut backend, &mut app)
    }));

    // ── Terminal teardown (always) ────────────────────────────────────────────
    let _ = disable_raw_mode();
    let _ = execute!(
        terminal.backend_mut(),
        DisableMouseCapture,
        DisableBracketedPaste,
        LeaveAlternateScreen,
    );
    let _ = terminal.show_cursor();

    match result {
        Ok(io_result) => io_result,
        Err(payload) => std::panic::resume_unwind(payload),
    }
}

/// Drive `app` against `backend` with passive auto-refresh.
///
/// This is identical to `quadraui::tui::run_inner` except for one change:
/// when [`TuiBackend::wait_events`] returns an empty `Vec` (timeout, no user
/// input), a synthetic `UiEvent::WindowFocused(true)` is appended so that
/// [`CoordApp::handle`] is still called and its internal 5-second refresh
/// timer can fire.  `WindowFocused` falls through to the `_ => {}` arm in
/// `handle`, so it produces no visible side effects until the timer fires.
fn run_with_tick(
    terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    backend: &mut TuiBackend,
    app: &mut CoordApp,
) -> io::Result<()> {
    app.setup(backend);
    let mut needs_redraw = true;

    loop {
        if needs_redraw {
            let size = terminal.size()?;
            backend.begin_frame(Viewport::new(
                size.width as f32,
                size.height as f32,
                1.0,
            ));
            terminal.draw(|frame| {
                backend.enter_frame_scope(frame, |b| {
                    app.render(b, <CoordApp as AppLogic>::AreaId::default());
                });
            })?;
            backend.end_frame();
            needs_redraw = false;
        }

        // Block up to POLL_TIMEOUT for real input events.
        let mut events = backend.wait_events(POLL_TIMEOUT);

        // Auto-refresh workaround (quadraui#236): when no real events
        // arrived, inject a neutral tick event so CoordApp::handle()'s
        // 5-second refresh timer can fire.  The event falls into `_ => {}`
        // in handle() and has zero visible effect except when the timer
        // expires, at which point handle() reloads the board data and
        // returns Reaction::Redraw.
        if events.is_empty() {
            events.push(UiEvent::WindowFocused(true));
        }

        for event in events {
            match app.handle(event, backend) {
                Reaction::Continue => {}
                Reaction::Redraw => needs_redraw = true,
                Reaction::Exit => return Ok(()),
            }
        }
    }
}
