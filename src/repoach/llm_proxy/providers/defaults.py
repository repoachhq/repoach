"""Default upstream base URLs and shared provider constants.

Adapters and :mod:`providers.registry` import from here to avoid duplicating
literals and to keep ``providers.registry`` free of per-adapter eager imports.

The trailing ``*_BASE_URL`` aliases are kept for backwards
compatibility with existing adapter modules that imported the old
names.
"""

NVIDIA_NIM_DEFAULT_BASE = "https://integrate.api.nvidia.com/v1"
OPENROUTER_DEFAULT_BASE = "https://openrouter.ai/api/v1"

KIMI_DEFAULT_BASE = "https://api.moonshot.ai/v1"
GROQ_DEFAULT_BASE = "https://api.groq.com/openai/v1"
CEREBRAS_DEFAULT_BASE = "https://api.cerebras.ai/v1"
DEEPSEEK_DEFAULT_BASE = "https://api.deepseek.com/v1"
OPENAI_DEFAULT_BASE = "https://api.openai.com/v1"
ANTHROPIC_DEFAULT_BASE = "https://api.anthropic.com"

NVIDIA_NIM_BASE_URL = NVIDIA_NIM_DEFAULT_BASE
OPENROUTER_BASE_URL = OPENROUTER_DEFAULT_BASE
