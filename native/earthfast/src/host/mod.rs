//! The game connection owned by Rust (the Rust port's phase 1: .claude/work/rust-port-plan-2026-11-15.md section 4):
//! the listening socket, the reads and the framing, the outgoing queue and the writes run on threads of their own,
//! without the GIL; Python's asyncio loop (protocol.HostStream) takes the frames in batches and runs the handlers.
#[allow(dead_code)]
pub mod frame;
