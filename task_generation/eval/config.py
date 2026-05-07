"""Path configuration, environment variable loading, and optional dependency initialisation.

All eval/ sub-modules import global constants from here to avoid repeated initialisation.
"""

import logging
import sys
from pathlib import Path

from dotenv import load_dotenv, find_dotenv


SCRIPT_DIR         = Path(__file__).resolve().parent.parent   # task_generation/
MCP_HALLU_DIR      = SCRIPT_DIR.parent
MCP_ATLAS_DIR      = MCP_HALLU_DIR / "mcp-atlas"
MCP_ATLAS_EVAL_DIR = MCP_ATLAS_DIR / "services" / "mcp_eval"

if str(MCP_ATLAS_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_ATLAS_EVAL_DIR))


_dotenv_path = MCP_ATLAS_DIR / ".env"
load_dotenv(_dotenv_path if _dotenv_path.exists() else find_dotenv(), override=True)


try:
    from mcp_evals_scores import (  # type: ignore
        AsyncLiteLLMClient as LLMClient,
        EvaluatorConfig as EvalCfg,
    )
    EVALS_AVAILABLE = True
except ImportError:
    LLMClient       = None  # type: ignore
    EvalCfg         = None  # type: ignore
    EVALS_AVAILABLE = False


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
