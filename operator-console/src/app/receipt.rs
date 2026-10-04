//! Terminal receipt export (issue #76 U9).
//!
//! `x` on a terminal run asks the Host to write the run's receipt (metadata
//! plus Run Root artifact references) to `<run root>/mission-run-receipt.json`.
//! The console never writes into the Run Root itself: the Host owns it, writes
//! the file atomically and answers with its path. Each export overwrites the
//! previous one, so repeating `x` is safe. The export re-reads the audit
//! artifact, so a written export refreshes the Overview once: the receipt
//! panel then shows the verdict the file carries even after terminal polling
//! stopped.

use super::{App, RefreshKey};
use crate::host::{HostCommand, HostError, OperatorSection, ReceiptExportOutcome, ReceiptExported};

/// Progress of the owner's receipt export for the current run.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub enum ReceiptExportState {
    #[default]
    Idle,
    /// The request is in flight; further `x` presses are ignored.
    Exporting,
    /// The Host wrote the file.
    Exported(ReceiptExported),
    /// The Host refused or the request failed.
    Failed(String),
}

impl App {
    /// `x`: ask the Host to export the terminal receipt.
    pub(crate) fn export_receipt(&mut self) {
        let Some(run) = self.run.as_ref().filter(|run| run.is_terminal()) else {
            return;
        };
        if self.view.receipt_export == ReceiptExportState::Exporting {
            return;
        }
        if !self.ownership_available() {
            self.hint =
                Some("Only the console that launched this run can export its receipt".into());
            return;
        }
        let has_receipt = self
            .view
            .overview
            .as_ref()
            .is_some_and(|overview| overview.receipt.is_some());
        if !has_receipt {
            self.hint = Some("This Host reports no receipt to export (API < 1.5)".into());
            return;
        }
        self.outbox.push(HostCommand::ExportReceipt {
            mission_run_id: run.mission_run_id.clone(),
            credential: self.session.credential.clone(),
        });
        self.view.receipt_export = ReceiptExportState::Exporting;
    }

    pub(crate) fn on_receipt_exported(&mut self, result: Result<ReceiptExportOutcome, HostError>) {
        if self.view.receipt_export != ReceiptExportState::Exporting {
            return;
        }
        let current = self.run.as_ref().map(|run| run.mission_run_id.as_str());
        self.view.receipt_export = match result {
            Ok(ReceiptExportOutcome::Exported(exported))
                if current == Some(exported.mission_run_id.as_str()) =>
            {
                ReceiptExportState::Exported(exported)
            }
            Ok(ReceiptExportOutcome::Exported(_)) => ReceiptExportState::Failed(
                "Receipt export answered for a different Mission Run".into(),
            ),
            Ok(ReceiptExportOutcome::Rejected { code, message }) => {
                ReceiptExportState::Failed(format!("Receipt export rejected ({code}): {message}"))
            }
            Err(error) => ReceiptExportState::Failed(format!("Receipt export failed: {error}")),
        };
        if matches!(self.view.receipt_export, ReceiptExportState::Exported(_)) {
            self.release_refresh(&RefreshKey::Section(OperatorSection::Overview));
            self.request_section(OperatorSection::Overview);
        }
    }
}
