"""Multi-agent memory for Mnemos.

Modules:
- shared_pool: a shared memory pool with visibility controls
- bridge: the cross-agent context bridge (``python -m mnemos.multiagent.bridge``)
"""

from .shared_pool import SharedPool
