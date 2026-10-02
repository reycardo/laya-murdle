"""Loading Laya: only the English checkpoint, straight from the local cache when it is there."""

from __future__ import annotations

import logging
import warnings

LAYA_REPO = "convaiinnovations/laya"

# Murdle clues are English. Naming the checkpoint on every call stops the Router's language
# detection from sending a clue to (and downloading) the multilingual one.
LAYA_MODEL = "english"

# The files laya.Agent downloads for the English checkpoint, which sits at the repo root.
CHECKPOINT_FILES = ["rl_agent_config.json", "model.safetensors", "tokenizer/*", "encoder/*"]

# The checkpoint's `choice:11+` temperature (0.1006) is out of range, so Laya clamps it to 0.5
# and warns on every load. Only choice questions with 11 or more options use it; ours have one
# per category member plus "none". Matches only when that is the sole entry Laya rejected.
CLAMPED_TEMPERATURE_WARNING = (
    r"laya: this checkpoint ships invalid temperatures .*; "
    r"using choice:11\+=[^,]* -> [^,]*\. Treat"
)

log = logging.getLogger(__name__)


def english_checkpoint() -> str:
    """The cached checkpoint folder, or the repo id when it has never been downloaded.

    Given a folder, laya.Agent loads it as-is, so a cached run makes no network calls at
    all - not even the freshness check a repo id costs on every start.
    """
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError

    try:
        folder = snapshot_download(
            LAYA_REPO, allow_patterns=CHECKPOINT_FILES, local_files_only=True
        )
    except LocalEntryNotFoundError:
        log.debug("checkpoint not cached, downloading %s from Hugging Face", LAYA_REPO)
        return LAYA_REPO
    log.debug("checkpoint cached at %s", folder)
    return folder


def load_router(preload: bool = True):
    from laya import Router

    # Installed process-wide, since --no-preload loads the checkpoint on the first predict.
    warnings.filterwarnings("ignore", CLAMPED_TEMPERATURE_WARNING, RuntimeWarning)
    router = Router(models={LAYA_MODEL: english_checkpoint()}, max_loaded=1)
    if preload:
        log.debug("preloading the %s checkpoint", LAYA_MODEL)
        router.preload([LAYA_MODEL])
        log.debug("checkpoint loaded")
    return router
