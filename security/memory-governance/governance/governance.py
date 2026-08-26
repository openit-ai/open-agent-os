"""Memory Governance — Section 27. Namespace + provenance + revoke invalidation."""
from enum import Enum
class MemoryScope(str, Enum):
    PERSONAL = "personal"   # user/{user_id}/*
    TEAM = "team"           # group/{group_id}/*
    CORPORATE = "corporate" # organization/*

# Provenance fields to store with every memory chunk:
# source_resource_id, source_acl_version, source_delegation_id, classification, retention_policy
# On delegation revoke: invalidate derived memories where source_delegation_id == revoked
