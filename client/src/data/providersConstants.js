/**
 * 9Router 全量 Provider 元数据 — 照搬自 9Router/master/open-sse/providers/registry/
 * 总计 18 Free + 15 OAuth + 62 apikey = 95 个 provider
 * 注：'free' 和 'freeTier' 在 9Router 里都用 'FREE' 不需密钥，这里合并为 FREE_TIER_PROVIDERS。
 * 'webCookie' 暂不接入（仅 2 个）。
 */

// ── 免费层 / 免授权（共 18 个，可无需密钥直接路由） ──
export const FREE_TIER_PROVIDERS = [
  {
    "id": "byteplus",
    "name": "BytePlus ModelArk",
    "alias": "byteplus",
    "color": "#2563EB",
    "icon": "cloud",
    "website": "https://console.byteplus.com/ark",
    "baseUrl": "https://ark.ap-southeast.bytepluses.com/api/coding/v3/chat/completions",
    "authModes": []
  },
  {
    "id": "cloudflare-ai",
    "name": "Cloudflare",
    "alias": "cloudflare-ai",
    "color": "#F38020",
    "icon": "cloud",
    "website": "https://developers.cloudflare.com/workers-ai/",
    "baseUrl": "https://api.cloudflare.com/client/v4/accounts/{accountId}/ai/v1/chat/completions",
    "authModes": []
  },
  {
    "id": "coqui",
    "name": "Coqui TTS",
    "alias": "coqui",
    "color": "#10B981",
    "icon": "record_voice_over",
    "website": "https://github.com/coqui-ai/TTS",
    "baseUrl": "http://localhost:5002/api/tts",
    "authModes": []
  },
  {
    "id": "edge-tts",
    "name": "Edge TTS",
    "alias": "edge-tts",
    "color": "#0078D4",
    "icon": "record_voice_over",
    "website": "",
    "baseUrl": "edge-tts",
    "authModes": []
  },
  {
    "id": "gemini-cli",
    "name": "Gemini CLI",
    "alias": "gc",
    "color": "#4285F4",
    "icon": "terminal",
    "website": "https://github.com/google-gemini/gemini-cli",
    "baseUrl": "https://cloudcode-pa.googleapis.com/v1internal",
    "authModes": []
  },
  {
    "id": "gemini",
    "name": "Gemini",
    "alias": "gemini",
    "color": "#4285F4",
    "icon": "diamond",
    "website": "https://ai.google.dev",
    "baseUrl": "https://generativelanguage.googleapis.com/v1beta/models",
    "authModes": []
  },
  {
    "id": "google-tts",
    "name": "Google TTS",
    "alias": "google-tts",
    "color": "#4285F4",
    "icon": "record_voice_over",
    "website": "",
    "baseUrl": "google-tts",
    "authModes": []
  },
  {
    "id": "kiro",
    "name": "Kiro AI",
    "alias": "kr",
    "color": "#FF6B35",
    "icon": "psychology_alt",
    "website": "https://kiro.dev",
    "baseUrl": "https://runtime.us-east-1.kiro.dev/generateAssistantResponse",
    "authModes": []
  },
  {
    "id": "local-device",
    "name": "Local Device",
    "alias": "local-device",
    "color": "#64748B",
    "icon": "speaker",
    "website": "",
    "baseUrl": "local-device",
    "authModes": []
  },
  {
    "id": "mimo-free",
    "name": "MiMo Code Free",
    "alias": "mmf",
    "color": "#FF6900",
    "icon": "smart_toy",
    "website": "",
    "baseUrl": "https://api.xiaomimimo.com/api/free-ai/openai/chat",
    "authModes": []
  },
  {
    "id": "nvidia",
    "name": "NVIDIA NIM",
    "alias": "nvidia",
    "color": "#76B900",
    "icon": "developer_board",
    "website": "https://developer.nvidia.com/nim",
    "baseUrl": "https://integrate.api.nvidia.com/v1/chat/completions",
    "authModes": []
  },
  {
    "id": "ollama",
    "name": "Ollama Cloud",
    "alias": "ollama",
    "color": "#ffffffff",
    "icon": "cloud",
    "website": "https://ollama.com",
    "baseUrl": "https://ollama.com/api/chat",
    "authModes": []
  },
  {
    "id": "opencode",
    "name": "OpenCode Free",
    "alias": "oc",
    "color": "#E87040",
    "icon": "terminal",
    "website": "",
    "baseUrl": "https://opencode.ai",
    "authModes": []
  },
  {
    "id": "openrouter",
    "name": "OpenRouter",
    "alias": "openrouter",
    "color": "#F97316",
    "icon": "router",
    "website": "https://openrouter.ai",
    "baseUrl": "https://openrouter.ai/api/v1/chat/completions",
    "authModes": []
  },
  {
    "id": "qoder",
    "name": "Qoder",
    "alias": "qd",
    "color": "#EC4899",
    "icon": "water_drop",
    "website": "https://qoder.com",
    "baseUrl": "https://api3.qoder.sh/algo/api/v2/service/pro/sse/agent_chat_generation",
    "authModes": []
  },
  {
    "id": "searxng",
    "name": "SearXNG",
    "alias": "searxng",
    "color": "#3B82F6",
    "icon": "saved_search",
    "website": "https://docs.searxng.org",
    "baseUrl": "http://localhost:8888/search",
    "authModes": []
  },
  {
    "id": "tortoise",
    "name": "Tortoise TTS",
    "alias": "tortoise",
    "color": "#7C3AED",
    "icon": "record_voice_over",
    "website": "https://github.com/neonbjb/tortoise-tts",
    "baseUrl": "http://localhost:5000/api/tts",
    "authModes": []
  },
  {
    "id": "vertex",
    "name": "Vertex AI",
    "alias": "vertex",
    "color": "#4285F4",
    "icon": "cloud",
    "website": "https://cloud.google.com/vertex-ai",
    "baseUrl": "https://aiplatform.googleapis.com",
    "authModes": []
  },
  {
    "id": "atomcode",
    "name": "AtomCode",
    "alias": "atomcode",
    "color": "#7C3AED",
    "icon": "smart_toy",
    "website": "https://atomcode.atomgit.com/invite/UVMWDFM7",
    "baseUrl": "https://llm-api.atomgit.com/v1",
    "api_type": "atomcode",
    "authInput": true,
    "models": ["deepseek-v4-flash", "GLM-5.1", "Qwen/Qwen3.6-35B-A3B", "Qwen/Qwen3-VL-8B-Instruct"],
    "authModes": ["api_key"],
    "note": "AIGate 直接反向代理 AtomGit LLM 网关（无需本地中转）。启用时粘贴你的 AtomCode 鉴权 JSON（默认读取路径 ~/.atomcode/auth.toml，可一键自动解析），需含 access_token / refresh_token / user.id"
  }
];

// ── OAuth 订阅（15 个，浏览器 PKCE 或导入 token） ──
export const OAUTH_PROVIDERS = [
  {
    "id": "antigravity",
    "name": "Antigravity",
    "alias": "ag",
    "color": "#F59E0B",
    "icon": "rocket_launch",
    "website": "https://antigravity.google",
    "baseUrl": "",
    "authModes": []
  },
  {
    "id": "claude",
    "name": "Claude Code",
    "alias": "cc",
    "color": "#D97757",
    "icon": "smart_toy",
    "website": "https://claude.ai",
    "baseUrl": "https://api.anthropic.com/v1/messages",
    "authModes": []
  },
  {
    "id": "cline",
    "name": "Cline",
    "alias": "cl",
    "color": "#5B9BD5",
    "icon": "smart_toy",
    "website": "https://cline.bot",
    "baseUrl": "https://api.cline.bot/api/v1/chat/completions",
    "authModes": []
  },
  {
    "id": "clinepass",
    "name": "ClinePass",
    "alias": "clinepass",
    "color": "#5B9BD5",
    "icon": "vpn_key",
    "website": "https://cline.bot",
    "baseUrl": "https://api.cline.bot/api/v1/chat/completions",
    "authModes": [
      "oauth",
      "apikey"
    ]
  },
  {
    "id": "codebuddy-cn",
    "name": "CodeBuddy CN",
    "alias": "cbcn",
    "color": "#006EFF",
    "icon": "smart_toy",
    "website": "https://copilot.tencent.com",
    "baseUrl": "https://copilot.tencent.com/v2/chat/completions",
    "authModes": [
      "oauth",
      "apikey"
    ]
  },
  {
    "id": "codex",
    "name": "OpenAI Codex",
    "alias": "cx",
    "color": "#3B82F6",
    "icon": "code",
    "website": "https://chatgpt.com/codex",
    "baseUrl": "https://chatgpt.com/backend-api/codex/responses",
    "authModes": []
  },
  {
    "id": "cursor",
    "name": "Cursor IDE",
    "alias": "cu",
    "color": "#00D4AA",
    "icon": "edit_note",
    "website": "https://cursor.com",
    "baseUrl": "https://api2.cursor.sh",
    "authModes": []
  },
  {
    "id": "github",
    "name": "GitHub Copilot",
    "alias": "gh",
    "color": "#333333",
    "icon": "code",
    "website": "https://github.com/features/copilot",
    "baseUrl": "https://api.githubcopilot.com/chat/completions",
    "authModes": []
  },
  {
    "id": "gitlab",
    "name": "GitLab Duo",
    "alias": "gitlab",
    "color": "#FC6D26",
    "icon": "code",
    "website": "https://gitlab.com",
    "baseUrl": "https://gitlab.com/api/v4/chat/completions",
    "authModes": []
  },
  {
    "id": "iflow",
    "name": "iFlow AI",
    "alias": "if",
    "color": "#6366F1",
    "icon": "water_drop",
    "website": "https://iflow.cn",
    "baseUrl": "https://apis.iflow.cn/v1/chat/completions",
    "authModes": []
  },
  {
    "id": "kilocode",
    "name": "Kilo Code",
    "alias": "kc",
    "color": "#FF6B35",
    "icon": "code",
    "website": "https://kilocode.ai",
    "baseUrl": "https://api.kilo.ai/api/openrouter/chat/completions",
    "authModes": []
  },
  {
    "id": "kimchi",
    "name": "Kimchi",
    "alias": "kimchi",
    "color": "#FF521D",
    "icon": "restaurant",
    "website": "https://kimchi.dev",
    "baseUrl": "https://llm.kimchi.dev/openai/v1/chat/completions",
    "authModes": [
      "oauth"
    ]
  },
  {
    "id": "kimi-coding",
    "name": "Kimi Coding",
    "alias": "kmc",
    "color": "#1E40AF",
    "icon": "psychology",
    "website": "https://kimi.moonshot.cn",
    "baseUrl": "https://api.kimi.com/coding/v1/messages",
    "authModes": []
  },
  {
    "id": "qwen",
    "name": "Qwen Code",
    "alias": "qw",
    "color": "#10B981",
    "icon": "psychology",
    "website": "https://chat.qwen.ai",
    "baseUrl": "https://portal.qwen.ai/v1/chat/completions",
    "authModes": []
  },
  {
    "id": "xai",
    "name": "xAI (Grok)",
    "alias": "xai",
    "color": "#1DA1F2",
    "icon": "auto_awesome",
    "website": "https://x.ai",
    "baseUrl": "https://api.x.ai/v1/chat/completions",
    "authModes": [
      "\n    \"oauth",
      "\n    \"apikey",
      "\n"
    ]
  }
];

// ── API Key 厂商（62 个，需粘贴显式密钥） ──
export const APIKEY_PROVIDERS = [
  {
    "id": "alicode-intl",
    "name": "Alibaba Intl",
    "alias": "alicode-intl",
    "color": "#FF6A00",
    "icon": "cloud",
    "website": "https://modelstudio.console.alibabacloud.com",
    "baseUrl": "https://coding-intl.dashscope.aliyuncs.com/v1/chat/completions",
    "authModes": []
  },
  {
    "id": "alicode",
    "name": "Alibaba",
    "alias": "alicode",
    "color": "#FF6A00",
    "icon": "cloud",
    "website": "https://bailian.console.aliyun.com",
    "baseUrl": "https://coding.dashscope.aliyuncs.com/v1/chat/completions",
    "authModes": []
  },
  {
    "id": "anthropic",
    "name": "Anthropic",
    "alias": "anthropic",
    "color": "#D97757",
    "icon": "smart_toy",
    "website": "https://console.anthropic.com",
    "baseUrl": "https://api.anthropic.com/v1/messages",
    "authModes": []
  },
  {
    "id": "assemblyai",
    "name": "AssemblyAI",
    "alias": "assemblyai",
    "color": "#0062FF",
    "icon": "record_voice_over",
    "website": "https://assemblyai.com",
    "baseUrl": "https://api.assemblyai.com/v1/audio/transcriptions",
    "authModes": []
  },
  {
    "id": "aws-polly",
    "name": "AWS Polly",
    "alias": "polly",
    "color": "#FF9900",
    "icon": "record_voice_over",
    "website": "https://aws.amazon.com/polly/",
    "baseUrl": "https://polly.{region}.amazonaws.com/v1/speech",
    "authModes": []
  },
  {
    "id": "azure",
    "name": "Azure OpenAI",
    "alias": "azure",
    "color": "#0078D4",
    "icon": "cloud",
    "website": "https://azure.microsoft.com/en-us/products/ai-services/openai-service",
    "baseUrl": "",
    "authModes": []
  },
  {
    "id": "black-forest-labs",
    "name": "Black Forest Labs",
    "alias": "black-forest-labs",
    "color": "#111827",
    "icon": "image",
    "website": "https://blackforestlabs.ai",
    "baseUrl": "https://api.bfl.ai/v1",
    "authModes": []
  },
  {
    "id": "blackbox",
    "name": "Blackbox AI",
    "alias": "blackbox",
    "color": "#5B5FEF",
    "icon": "smart_toy",
    "website": "https://blackbox.ai",
    "baseUrl": "https://api.blackbox.ai/v1/chat/completions",
    "authModes": []
  },
  {
    "id": "brave-search",
    "name": "Brave Search",
    "alias": "brave",
    "color": "#FB542B",
    "icon": "travel_explore",
    "website": "https://brave.com/search/api",
    "baseUrl": "https://api.search.brave.com/res/v1",
    "authModes": []
  },
  {
    "id": "cartesia",
    "name": "Cartesia",
    "alias": "cartesia",
    "color": "#FF4F8B",
    "icon": "spatial_audio",
    "website": "https://cartesia.ai",
    "baseUrl": "https://api.cartesia.ai/tts/bytes",
    "authModes": []
  },
  {
    "id": "cerebras",
    "name": "Cerebras",
    "alias": "cerebras",
    "color": "#FF4F00",
    "icon": "memory",
    "website": "https://www.cerebras.ai",
    "baseUrl": "https://api.cerebras.ai/v1/chat/completions",
    "authModes": []
  },
  {
    "id": "chutes",
    "name": "Chutes AI",
    "alias": "chutes",
    "color": "#ffffffff",
    "icon": "water_drop",
    "website": "https://chutes.ai",
    "baseUrl": "https://llm.chutes.ai/v1/chat/completions",
    "authModes": []
  },
  {
    "id": "cohere",
    "name": "Cohere",
    "alias": "cohere",
    "color": "#39594D",
    "icon": "hub",
    "website": "https://cohere.com",
    "baseUrl": "https://api.cohere.ai/v1/chat/completions",
    "authModes": []
  },
  {
    "id": "comfyui",
    "name": "ComfyUI",
    "alias": "comfyui",
    "color": "#4CAF50",
    "icon": "account_tree",
    "website": "https://github.com/comfyanonymous/ComfyUI",
    "baseUrl": "http://localhost:8188",
    "authModes": []
  },
  {
    "id": "commandcode",
    "name": "Command Code",
    "alias": "commandcode",
    "color": "#000000",
    "icon": "smart_toy",
    "website": "https://commandcode.ai",
    "baseUrl": "https://api.commandcode.ai/alpha/generate",
    "authModes": []
  },
  {
    "id": "deepgram",
    "name": "Deepgram",
    "alias": "deepgram",
    "color": "#13EF93",
    "icon": "mic",
    "website": "https://deepgram.com",
    "baseUrl": "https://api.deepgram.com/v1/listen",
    "authModes": []
  },
  {
    "id": "deepseek",
    "name": "DeepSeek",
    "alias": "deepseek",
    "color": "#4D6BFE",
    "icon": "bolt",
    "website": "https://deepseek.com",
    "baseUrl": "https://api.deepseek.com/chat/completions",
    "authModes": []
  },
  {
    "id": "elevenlabs",
    "name": "ElevenLabs",
    "alias": "el",
    "color": "#6C47FF",
    "icon": "record_voice_over",
    "website": "https://elevenlabs.io",
    "baseUrl": "https://api.elevenlabs.io/v1/text-to-speech",
    "authModes": []
  },
  {
    "id": "exa",
    "name": "Exa",
    "alias": "exa",
    "color": "#2563EB",
    "icon": "manage_search",
    "website": "https://exa.ai",
    "baseUrl": "https://api.exa.ai/search",
    "authModes": []
  },
  {
    "id": "fal-ai",
    "name": "Fal.ai",
    "alias": "fal-ai",
    "color": "#2563EB",
    "icon": "image",
    "website": "https://fal.ai",
    "baseUrl": "https://queue.fal.run",
    "authModes": []
  },
  {
    "id": "firecrawl",
    "name": "Firecrawl",
    "alias": "firecrawl",
    "color": "#F59E0B",
    "icon": "local_fire_department",
    "website": "https://firecrawl.dev",
    "baseUrl": "https://api.firecrawl.dev/v1/scrape",
    "authModes": []
  },
  {
    "id": "fireworks",
    "name": "Fireworks AI",
    "alias": "fireworks",
    "color": "#7B2EF2",
    "icon": "local_fire_department",
    "website": "https://fireworks.ai",
    "baseUrl": "https://api.fireworks.ai/inference/v1/chat/completions",
    "authModes": []
  },
  {
    "id": "glm-cn",
    "name": "GLM (China)",
    "alias": "glm-cn",
    "color": "#DC2626",
    "icon": "code",
    "website": "https://open.bigmodel.cn",
    "baseUrl": "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions",
    "authModes": []
  },
  {
    "id": "glm",
    "name": "GLM Coding",
    "alias": "glm",
    "color": "#2563EB",
    "icon": "code",
    "website": "https://open.bigmodel.cn",
    "baseUrl": "https://api.z.ai/api/anthropic/v1/messages",
    "authModes": []
  },
  {
    "id": "google-pse",
    "name": "Google PSE",
    "alias": "gpse",
    "color": "#4285F4",
    "icon": "search",
    "website": "https://programmablesearchengine.google.com",
    "baseUrl": "https://www.googleapis.com/customsearch/v1",
    "authModes": []
  },
  {
    "id": "groq",
    "name": "Groq",
    "alias": "groq",
    "color": "#F55036",
    "icon": "speed",
    "website": "https://groq.com",
    "baseUrl": "https://api.groq.com/openai/v1/chat/completions",
    "authModes": []
  },
  {
    "id": "huggingface",
    "name": "HuggingFace",
    "alias": "huggingface",
    "color": "#FFD21E",
    "icon": "face",
    "website": "https://huggingface.co",
    "baseUrl": "https://api-inference.huggingface.co/models",
    "authModes": []
  },
  {
    "id": "hyperbolic",
    "name": "Hyperbolic",
    "alias": "hyperbolic",
    "color": "#00D4FF",
    "icon": "bolt",
    "website": "https://hyperbolic.xyz",
    "baseUrl": "https://api.hyperbolic.xyz/v1/chat/completions",
    "authModes": []
  },
  {
    "id": "inworld",
    "name": "Inworld TTS",
    "alias": "inworld",
    "color": "#FF6B6B",
    "icon": "record_voice_over",
    "website": "https://inworld.ai",
    "baseUrl": "https://api.inworld.ai/tts/v1/voice",
    "authModes": []
  },
  {
    "id": "jina-ai",
    "name": "Jina AI",
    "alias": "jina",
    "color": "#2563EB",
    "icon": "blur_on",
    "website": "https://jina.ai",
    "baseUrl": "https://api.jina.ai/v1/embeddings",
    "authModes": []
  },
  {
    "id": "jina-reader",
    "name": "Jina Reader",
    "alias": "jina-reader",
    "color": "#000000",
    "icon": "menu_book",
    "website": "https://jina.ai/reader",
    "baseUrl": "https://r.jina.ai",
    "authModes": []
  },
  {
    "id": "kimi",
    "name": "Kimi",
    "alias": "kimi",
    "color": "#1E3A8A",
    "icon": "psychology",
    "website": "https://kimi.moonshot.cn",
    "baseUrl": "https://api.kimi.com/coding/v1/messages",
    "authModes": []
  },
  {
    "id": "linkup",
    "name": "Linkup",
    "alias": "linkup",
    "color": "#0EA5E9",
    "icon": "link",
    "website": "https://linkup.so",
    "baseUrl": "https://api.linkup.so/v1/search",
    "authModes": []
  },
  {
    "id": "minimax-cn",
    "name": "Minimax (China)",
    "alias": "minimax-cn",
    "color": "#DC2626",
    "icon": "memory",
    "website": "https://www.minimaxi.com",
    "baseUrl": "https://api.minimaxi.com/anthropic/v1/messages",
    "authModes": []
  },
  {
    "id": "minimax",
    "name": "Minimax Coding",
    "alias": "minimax",
    "color": "#7C3AED",
    "icon": "memory",
    "website": "https://www.minimaxi.com",
    "baseUrl": "https://api.minimax.io/anthropic/v1/messages",
    "authModes": []
  },
  {
    "id": "mistral",
    "name": "Mistral",
    "alias": "mistral",
    "color": "#FF7000",
    "icon": "air",
    "website": "https://mistral.ai",
    "baseUrl": "https://api.mistral.ai/v1/chat/completions",
    "authModes": []
  },
  {
    "id": "mmf",
    "name": "MMF",
    "alias": "mmf",
    "color": "#6366F1",
    "icon": "hub",
    "website": "",
    "baseUrl": "https://api.xiaomimimo.com/api/free-ai/openai/chat",
    "authModes": []
  },
  {
    "id": "nanobanana",
    "name": "NanoBanana API",
    "alias": "nanobanana",
    "color": "#FFD700",
    "icon": "extension",
    "website": "https://nanobananaapi.ai",
    "baseUrl": "https://api.nanobananaapi.ai/v1/chat/completions",
    "authModes": []
  },
  {
    "id": "nebius",
    "name": "Nebius AI",
    "alias": "nebius",
    "color": "#6C5CE7",
    "icon": "cloud",
    "website": "https://nebius.com",
    "baseUrl": "https://api.studio.nebius.ai/v1/chat/completions",
    "authModes": []
  },
  {
    "id": "ollama-local",
    "name": "Ollama Local",
    "alias": "ollama-local",
    "color": "#ffffffff",
    "icon": "cloud",
    "website": "https://ollama.com",
    "baseUrl": "http://localhost:11434/api/chat",
    "authModes": []
  },
  {
    "id": "openai",
    "name": "OpenAI",
    "alias": "openai",
    "color": "#10A37F",
    "icon": "auto_awesome",
    "website": "https://platform.openai.com",
    "baseUrl": "https://api.openai.com/v1/chat/completions",
    "authModes": []
  },
  {
    "id": "opencode-go",
    "name": "OpenCode Go",
    "alias": "opencode-go",
    "color": "#E87040",
    "icon": "terminal",
    "website": "https://opencode.ai/auth",
    "baseUrl": "https://opencode.ai/zen/go/v1/chat/completions",
    "authModes": []
  },
  {
    "id": "perplexity",
    "name": "Perplexity",
    "alias": "perplexity",
    "color": "#20808D",
    "icon": "search",
    "website": "https://www.perplexity.ai",
    "baseUrl": "https://api.perplexity.ai/chat/completions",
    "authModes": []
  },
  {
    "id": "playht",
    "name": "PlayHT",
    "alias": "playht",
    "color": "#00B4D8",
    "icon": "play_circle",
    "website": "https://play.ht",
    "baseUrl": "https://api.play.ht/api/v2/tts/stream",
    "authModes": []
  },
  {
    "id": "recraft",
    "name": "Recraft",
    "alias": "recraft",
    "color": "#EC4899",
    "icon": "image",
    "website": "https://recraft.ai",
    "baseUrl": "https://external.api.recraft.ai/v1/images/generations",
    "authModes": []
  },
  {
    "id": "runwayml",
    "name": "Runway ML",
    "alias": "runwayml",
    "color": "#000000",
    "icon": "movie",
    "website": "https://runwayml.com",
    "baseUrl": "https://api.dev.runwayml.com/v1",
    "authModes": []
  },
  {
    "id": "sdwebui",
    "name": "SD WebUI",
    "alias": "sdwebui",
    "color": "#FF7043",
    "icon": "brush",
    "website": "https://github.com/AUTOMATIC1111/stable-diffusion-webui",
    "baseUrl": "http://localhost:7860/sdapi/v1/txt2img",
    "authModes": []
  },
  {
    "id": "searchapi",
    "name": "SearchAPI",
    "alias": "searchapi",
    "color": "#0EA5A4",
    "icon": "search",
    "website": "https://www.searchapi.io",
    "baseUrl": "https://www.searchapi.io/api/v1/search",
    "authModes": []
  },
  {
    "id": "serper",
    "name": "Serper",
    "alias": "serper",
    "color": "#4F46E5",
    "icon": "search",
    "website": "https://serper.dev",
    "baseUrl": "https://google.serper.dev",
    "authModes": []
  },
  {
    "id": "siliconflow",
    "name": "SiliconFlow",
    "alias": "siliconflow",
    "color": "#5B6EF5",
    "icon": "cloud_queue",
    "website": "https://cloud.siliconflow.com",
    "baseUrl": "https://api.siliconflow.com/v1/chat/completions",
    "authModes": []
  },
  {
    "id": "stability-ai",
    "name": "Stability AI",
    "alias": "stability-ai",
    "color": "#8B5CF6",
    "icon": "image",
    "website": "https://stability.ai",
    "baseUrl": "https://api.stability.ai/v2beta/stable-image/generate",
    "authModes": []
  },
  {
    "id": "tavily",
    "name": "Tavily",
    "alias": "tavily",
    "color": "#5B21B6",
    "icon": "search",
    "website": "https://tavily.com",
    "baseUrl": "https://api.tavily.com/search",
    "authModes": []
  },
  {
    "id": "together",
    "name": "Together AI",
    "alias": "together",
    "color": "#0F6FFF",
    "icon": "group_work",
    "website": "https://www.together.ai",
    "baseUrl": "https://api.together.xyz/v1/chat/completions",
    "authModes": []
  },
  {
    "id": "topaz",
    "name": "Topaz",
    "alias": "topaz",
    "color": "#059669",
    "icon": "image",
    "website": "https://topazlabs.com",
    "baseUrl": "",
    "authModes": []
  },
  {
    "id": "venice",
    "name": "Venice AI",
    "alias": "venice",
    "color": "#DC2626",
    "icon": "shield",
    "website": "https://venice.ai",
    "baseUrl": "https://api.venice.ai/api/v1/chat/completions",
    "authModes": []
  },
  {
    "id": "vercel-ai-gateway",
    "name": "Vercel AI Gateway",
    "alias": "vercel-ai-gateway",
    "color": "#111827",
    "icon": "deployed_code",
    "website": "https://vercel.com/ai-gateway",
    "baseUrl": "https://ai-gateway.vercel.sh/v1/chat/completions",
    "authModes": []
  },
  {
    "id": "vertex-partner",
    "name": "Vertex Partner",
    "alias": "vertex-partner",
    "color": "#34A853",
    "icon": "cloud",
    "website": "https://cloud.google.com/vertex-ai/generative-ai/docs/partner-models/use-partner-models",
    "baseUrl": "https://aiplatform.googleapis.com",
    "authModes": []
  },
  {
    "id": "volcengine-ark",
    "name": "Volcengine Ark",
    "alias": "volcengine-ark",
    "color": "#1677FF",
    "icon": "cloud",
    "website": "https://ark.cn-beijing.volces.com",
    "baseUrl": "https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions",
    "authModes": []
  },
  {
    "id": "voyage-ai",
    "name": "Voyage AI",
    "alias": "voyage-ai",
    "color": "#0EA5E9",
    "icon": "data_array",
    "website": "https://www.voyageai.com",
    "baseUrl": "https://api.voyageai.com/v1/embeddings",
    "authModes": []
  },
  {
    "id": "xiaomi-mimo",
    "name": "Xiaomi MiMo",
    "alias": "xiaomi-mimo",
    "color": "#FF6900",
    "icon": "smart_toy",
    "website": "https://xiaomimimo.com",
    "baseUrl": "https://api.xiaomimimo.com/v1/chat/completions",
    "authModes": []
  },
  {
    "id": "xiaomi-tokenplan",
    "name": "Xiaomi MiMo (Token Plan)",
    "alias": "xiaomi-tokenplan",
    "color": "#FF6700",
    "icon": "smart_toy",
    "website": "https://mimo.xiaomi.com",
    "baseUrl": "https://token-plan-sgp.xiaomimimo.com/v1/chat/completions",
    "authModes": []
  },
  {
    "id": "youcom",
    "name": "You.com Search",
    "alias": "youcom",
    "color": "#7C3AED",
    "icon": "search",
    "website": "https://you.com",
    "baseUrl": "https://ydc-index.io/v1/search",
    "authModes": []
  }
];

// 全部
export const ALL_PROVIDERS = [...FREE_TIER_PROVIDERS, ...OAUTH_PROVIDERS, ...APIKEY_PROVIDERS];

export function providerByAlias(alias) {
  for (const p of ALL_PROVIDERS) {
    if (p.alias === alias || p.id === alias) return p;
  }
  return null;
}

export function categorize(alias) {
  const p = providerByAlias(alias);
  if (!p) return 'apikey';
  if (FREE_TIER_PROVIDERS.includes(p)) return 'free_tier';
  if (OAUTH_PROVIDERS.includes(p)) return 'oauth';
  return 'apikey';
}