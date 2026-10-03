//! Runtime Host boundary: wire DTOs, the blocking HTTP client, and the
//! worker threads that keep IO off the UI thread.

pub mod client;
pub mod dto;
pub mod workers;

pub use client::{HostClient, HostError, UreqHostClient};
pub use dto::*;
pub use workers::{ContentPurpose, HostCommand, HostMessage, Workers};
