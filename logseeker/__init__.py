"""LogSeeker — self-service retrieval of order logs from the algo log servers.

Replaces the legacy WinSCP + .bat + remote-ksh chain (see ../legacy/) with a
single Streamlit app that talks to the log host over SSH and streams matches
straight back to the requester.
"""

__version__ = "1.0.0"
