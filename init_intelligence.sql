-- 初始化智力评分表（AIGate 启动时会自动建表，这里是预置数据）
-- 使用方法: sqlite3 data/aigate.db < init_intelligence.sql
-- 或者直接粘贴到 SQLite 客户端执行

CREATE TABLE IF NOT EXISTS intelligence_static (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL UNIQUE,
    score INTEGER NOT NULL DEFAULT 60,
    tier TEXT NOT NULL DEFAULT 'B',
    notes TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO intelligence_static (pattern, score, tier, notes) VALUES
-- S 级 (>85)
('claude-opus-4-*', 92, 'S', 'Claude Opus 4 系列 - 顶尖推理编码'),
('gpt-5.5*', 90, 'S', 'GPT-5.5 - OpenAI 最新旗舰'),
('gpt-5.4*', 87, 'S', 'GPT-5.4 系列'),
('deepseek-v4-*', 88, 'S', 'DeepSeek V4 系列'),
('deepseek-reasoner', 85, 'S', 'DeepSeek Reasoner'),
('kimi-k2.7', 85, 'S', 'Kimi K2.7'),
('gemini-2*', 87, 'S', 'Gemini 2.x 系列'),
('deepseek-ai/deepseek-v4-*', 88, 'S', 'DeepSeek V4 (带前缀)'),
-- A 级 (70-85)
('claude-sonnet-4-*', 80, 'A', 'Claude Sonnet 4'),
('grok-4.3-*', 82, 'A', 'Grok 4.3 系列'),
('grok-4.20-*', 78, 'A', 'Grok 4.20'),
('kimi-k2.6', 78, 'A', 'Kimi K2.6'),
('glm-5.1', 76, 'A', 'ChatGLM 5.1'),
('glm-5', 72, 'A', 'ChatGLM 5'),
('gpt-4o*', 80, 'A', 'GPT-4o'),
('gemini-1.5-*', 80, 'A', 'Gemini 1.5 系列'),
('qwen/qwen3.5-*', 73, 'A', 'Qwen3.5 大参数量'),
('qwen/qwen3-next-*', 74, 'A', 'Qwen3 Next'),
('minimaxai/minimax-m3', 79, 'A', 'MiniMax M3'),
('minimaxai/minimax-m2.7', 77, 'A', 'MiniMax M2.7'),
('moonshotai/kimi-k2.6', 78, 'A', 'Moonshot Kimi K2.6'),
('deepseek-chat', 72, 'A', 'DeepSeek Chat'),
-- B 级 (50-70)
('kimi-k2.5', 68, 'B', 'Kimi K2.5'),
('gpt-4o-mini', 62, 'B', 'GPT-4o Mini'),
('llama-4-maverick*', 66, 'B', 'Llama 4 Maverick'),
('llama-3.1-*70b*', 68, 'B', 'Llama 3.1 70B'),
('llama-3.3-*', 70, 'B', 'Llama 3.3'),
('nvidia/nemotron-4-*', 67, 'B', 'Nemotron 4 340B'),
('nvidia/nemotron-3-super-*', 71, 'B', 'Nemotron 3 Super'),
('qwen/qwen3.5-122b*', 70, 'B', 'Qwen3.5 122B'),
('mistralai/mistral-large*', 70, 'B', 'Mistral Large'),
('mistralai/mistral-medium*', 67, 'B', 'Mistral Medium'),
('stepfun-ai/step-3*', 69, 'B', 'Step 3.x'),
('google/gemma-3n-*', 52, 'B', 'Gemma 3N'),
-- C 级 (<50)
('google/gemma-*-it', 42, 'C', 'Gemma IT 系列'),
('nvidia/nemotron-nano-*', 45, 'C', 'Nemotron Nano'),
('nvidia/nemotron-mini-*', 44, 'C', 'Nemotron Mini'),
('meta/llama-3.2-*', 45, 'C', 'Llama 3.2'),
('mistralai/mistral-7b*', 42, 'C', 'Mistral 7B'),
('microsoft/phi-*', 44, 'C', 'Phi 系列'),
('ibm/granite-*', 41, 'C', 'Granite'),
('google/gemma-2*', 40, 'C', 'Gemma 2'),
('google/codegemma*', 38, 'C', 'CodeGemma');
