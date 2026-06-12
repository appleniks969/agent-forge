"""front: the shells and the composition root.

Layer: front — imports everything; nothing imports it. wiring.py is the
composition root and owns every os.environ read; repl.py and oneshot.py are
the two shells (renderer + Asker over SessionHandle); render.py draws
envelopes; commands.py is the declarative slash command table they share.
Import names from their defining modules — no re-exports here.
"""

from __future__ import annotations
