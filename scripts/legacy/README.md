# Historical operation archive

These one-off migration, review, and live-probe scripts describe development history. They are **not installers** and cannot be executed directly. Use [the supported setup guide](../../docs/AGENT-SETUP.md).

Files ending in `.py.txt` are reference text and unconditionally refuse execution, including through an explicit loader. The few `.py` files remain importable for offline unit tests of their pure validation helpers; their command-line entrypoints refuse execution before doing work. Old host/account targets have been replaced with non-operational example values. Archived host guards remain in place. Do not remove those guards to use these scripts on a new computer.

Historical live receipts and private source snapshots are not distributed. Tests which need those optional fixtures explicitly skip when they are absent; a skip is not evidence of a successful live qualification.
