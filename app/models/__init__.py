from app.models.amo import AmoLead, AmoLossReason, AmoNote, AmoPipeline, AmoStatus, AmoTask, AmoUser
from app.models.hh import HhCandidate, HhNegotiation, HhOAuthToken, HhVacancy
from app.models.sync import SyncState

__all__ = [
    "AmoLead", "AmoLossReason", "AmoNote", "AmoPipeline", "AmoStatus", "AmoTask", "AmoUser",
    "HhCandidate", "HhNegotiation", "HhOAuthToken", "HhVacancy",
    "SyncState",
]
