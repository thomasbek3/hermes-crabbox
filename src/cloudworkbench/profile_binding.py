"""Canonical identity for the five-field pinned provider CLI profile."""
from dataclasses import asdict
import hashlib
import json


def canonical_pinned_profile_digest(profile):
    value=asdict(profile);value['path']=str(profile.path)
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

