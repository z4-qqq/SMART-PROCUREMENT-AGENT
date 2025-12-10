from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from main import build_procurement_plan  # type: ignore

load_dotenv()

logger = logging.getLogger("procurement_web")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = FastAPI(title="Smart Procurement Agent")

# Если будешь открывать фронт с другого origin — CORS пригодится
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # на демо можно звёздочку, потом можно ужесточить
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Простое in-memory хранилище истории диалогов:
# conversation_id -> список сообщений в формате OpenAI-чата
# [{"role": "user"|"assistant", "content": "..."}]
conversations: Dict[str, List[Dict[str, str]]] = {}


class ChatRequest(BaseModel):
    message: str = Field(..., description="Сообщение пользователя (запрос на закупку)")
    conversation_id: Optional[str] = Field(
        default=None,
        description=(
            "ID диалога (для продолжения разговора). "
            "Если не указан — будет создан новый."
        ),
    )


class ChatResponse(BaseModel):
    summary: str = Field(..., description="Человекочитаемый ответ агента")
    plan: Dict[str, Any] = Field(
        ..., description="Полный JSON-план закупки для интеграций/отладки"
    )
    conversation_id: str = Field(
        ..., description="ID диалога, который нужно использовать для следующих сообщений."
    )


# ----------------- HTML (простая страница чата) -----------------

HTML_PAGE = """
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <title>Smart Procurement Agent</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root {
      --bg: #0f172a;
      --bg-elevated: #020617;
      --accent: #38bdf8;
      --accent-soft: rgba(56, 189, 248, 0.12);
      --text: #e5e7eb;
      --text-muted: #9ca3af;
      --danger: #f97373;
    }
    * {
      box-sizing: border-box;
    }
    body {
      margin: 0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: radial-gradient(circle at top, #0b1120 0, #020617 45%, #020617 100%);
      color: var(--text);
      height: 100vh;
      display: flex;
      align-items: stretch;
      justify-content: center;
    }
    #app {
      width: 100%;
      max-width: 960px;
      margin: 16px;
      border-radius: 18px;
      background: linear-gradient(145deg, rgba(15,23,42,0.97), rgba(15,23,42,0.99));
      box-shadow:
        0 20px 60px rgba(15,23,42,0.7),
        0 0 0 1px rgba(148,163,184,0.15);
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    header {
      padding: 14px 18px;
      border-bottom: 1px solid rgba(148,163,184,0.25);
      display: flex;
      align-items: center;
      justify-content: space-between;
      background: radial-gradient(circle at top left, rgba(56,189,248,0.16), transparent 55%);
    }
    header .title {
      font-size: 16px;
      font-weight: 600;
      display: flex;
      align-items: center;
      gap: 10px;
    }
    header .pill {
      font-size: 11px;
      padding: 2px 8px;
      border-radius: 999px;
      background: rgba(15,23,42,0.7);
      border: 1px solid rgba(148,163,184,0.6);
      color: var(--text-muted);
    }
    header .status {
      font-size: 12px;
      color: var(--text-muted);
      display: flex;
      align-items: center;
      gap: 8px;
    }
    header .dot {
      width: 9px;
      height: 9px;
      border-radius: 999px;
      background: #22c55e;
      box-shadow: 0 0 12px rgba(34,197,94,0.9);
    }

    #chat {
      flex: 1;
      padding: 16px 18px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 10px;
      background:
        radial-gradient(circle at top left, rgba(30,64,175,0.32), transparent 50%),
        radial-gradient(circle at bottom right, rgba(17,94,89,0.25), transparent 55%);
    }

    .message-row {
      display: flex;
      margin-bottom: 4px;
    }
    .message-row.user {
      justify-content: flex-end;
    }
    .message-row.assistant {
      justify-content: flex-start;
    }

    .bubble {
      max-width: 78%;
      padding: 10px 12px;
      border-radius: 14px;
      font-size: 14px;
      line-height: 1.45;
      position: relative;
      border: 1px solid transparent;
      white-space: pre-wrap;
      word-wrap: break-word;
      overflow-wrap: break-word;
    }
    .bubble.user {
      background: linear-gradient(135deg, #38bdf8, #22c55e);
      color: #0b1120;
      border-color: rgba(15,23,42,0.4);
    }
    .bubble.assistant {
      background: rgba(15,23,42,0.9);
      border-color: rgba(148,163,184,0.5);
      color: var(--text);
    }

    .bubble .label {
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--text-muted);
      margin-bottom: 4px;
      opacity: 0.9;
    }

    .bubble .meta {
      margin-top: 6px;
      font-size: 11px;
      color: var(--text-muted);
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }

    .bubble details {
      margin-top: 8px;
      background: rgba(15,23,42,0.9);
      border-radius: 10px;
      border: 1px solid rgba(148,163,184,0.4);
      padding: 6px 8px 8px;
    }
    .bubble summary {
      cursor: pointer;
      font-size: 12px;
      color: var(--accent);
      list-style: none;
    }
    .bubble summary::-webkit-details-marker {
      display: none;
    }
    .bubble summary::before {
      content: "▶";
      display: inline-block;
      font-size: 9px;
      margin-right: 6px;
      transform: translateY(-1px);
      opacity: 0.8;
    }
    details[open] summary::before {
      content: "▼";
    }
    .bubble pre {
      margin: 6px 0 0;
      font-size: 11px;
      max-height: 220px;
      overflow: auto;
      background: #020617;
      border-radius: 8px;
      padding: 8px;
      border: 1px solid rgba(30,41,59,0.85);
      color: #e5e7eb;
    }

    .system-note {
      font-size: 12px;
      color: var(--text-muted);
      margin-bottom: 6px;
    }

    #input-container {
      border-top: 1px solid rgba(148,163,184,0.4);
      padding: 10px 12px;
      background: radial-gradient(circle at bottom, rgba(15,23,42,0.96), #020617 70%);
    }

    #chat-form {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    #user-input {
      width: 100%;
      min-height: 60px;
      max-height: 160px;
      resize: vertical;
      border-radius: 10px;
      border: 1px solid rgba(148,163,184,0.6);
      padding: 8px 10px;
      background: #020617;
      color: var(--text);
      font-size: 14px;
      outline: none;
    }
    #user-input::placeholder {
      color: rgba(148,163,184,0.9);
    }

    .form-footer {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
    }
    .hint {
      font-size: 11px;
      color: var(--text-muted);
    }
    .hint code {
      font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      background: rgba(15,23,42,0.9);
      padding: 1px 4px;
      border-radius: 4px;
      border: 1px solid rgba(148,163,184,0.5);
    }

    button {
      border-radius: 999px;
      padding: 7px 14px;
      border: none;
      font-size: 13px;
      font-weight: 500;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: linear-gradient(135deg, #38bdf8, #22c55e);
      color: #020617;
      box-shadow:
        0 8px 25px rgba(34,197,94,0.35),
        0 0 0 1px rgba(15,23,42,0.4);
      transition: transform 0.06s ease, box-shadow 0.08s ease, opacity 0.1s ease;
    }
    button:disabled {
      opacity: 0.55;
      cursor: default;
      box-shadow: none;
    }
    button:not(:disabled):hover {
      transform: translateY(-0.5px);
      box-shadow:
        0 10px 30px rgba(56,189,248,0.4),
        0 0 0 1px rgba(15,23,42,0.6);
    }

    #loading-indicator {
      display: none;
      font-size: 11px;
      color: var(--text-muted);
      align-items: center;
      gap: 6px;
    }
    #loading-indicator .spinner {
      width: 12px;
      height: 12px;
      border-radius: 999px;
      border: 2px solid rgba(148,163,184,0.6);
      border-top-color: var(--accent);
      animation: spin 0.7s linear infinite;
    }

    @keyframes spin {
      to { transform: rotate(360deg); }
    }

    @media (max-width: 640px) {
      #app {
        margin: 8px;
        border-radius: 14px;
      }
      header {
        padding: 10px 12px;
      }
      #chat {
        padding: 12px;
      }
    }
  </style>
</head>
<body>
  <div id="app">
    <header>
      <div class="title">
        <span>🤖 Smart Procurement Agent</span>
        <span class="pill">MCP · Printful</span>
      </div>
      <div class="status">
        <span class="dot"></span>
        <span id="status-text">Готов к запросу</span>
      </div>
    </header>

    <main id="chat">
      <div class="system-note">
        💡 Опиши, какой мерч или промо-товары нужно закупить (худи, футболки, кружки и т.п.), можно указать бюджет и вебхук.
        Например: «Сделай мерч к конференции на 50 человек: худи, футболки и кружки, покажи итог в EUR и отправь план в мой вебхук https://example.com/hook».
      </div>
    </main>

    <div id="input-container">
      <form id="chat-form">
        <textarea
          id="user-input"
          placeholder="Опиши задачу закупки мерча…"
        ></textarea>
        <div class="form-footer">
          <div class="hint">
            Нажми <code>Ctrl+Enter</code>, чтобы отправить сообщение.
            <div id="loading-indicator">
              <div class="spinner"></div>
              <span>Агент считает предложения поставщиков…</span>
            </div>
          </div>
          <button type="submit" id="send-btn">
            <span>Отправить</span>
          </button>
        </div>
      </form>
    </div>
  </div>

  <script>
    const chat = document.getElementById('chat');
    const form = document.getElementById('chat-form');
    const input = document.getElementById('user-input');
    const sendBtn = document.getElementById('send-btn');
    const statusText = document.getElementById('status-text');
    const loadingIndicator = document.getElementById('loading-indicator');

    let isBusy = false;
    let conversationId = null;

    function setLoading(loading) {
      isBusy = loading;
      sendBtn.disabled = loading;
      loadingIndicator.style.display = loading ? 'inline-flex' : 'none';
      statusText.textContent = loading ? 'Агент думает…' : 'Готов к запросу';
    }

    function scrollToBottom() {
      requestAnimationFrame(() => {
        chat.scrollTop = chat.scrollHeight;
      });
    }

    function createBubble(role, text, plan) {
      const row = document.createElement('div');
      row.className = 'message-row ' + role;

      const bubble = document.createElement('div');
      bubble.className = 'bubble ' + role;

      const label = document.createElement('div');
      label.className = 'label';
      label.textContent = role === 'user' ? 'Вы' : 'Smart Procurement Agent';
      bubble.appendChild(label);

      const body = document.createElement('div');
      body.textContent = text;
      bubble.appendChild(body);

      if (role === 'assistant' && plan) {
        const meta = document.createElement('div');
        meta.className = 'meta';

        const total = plan.totals_target_currency || plan.totals_supplier_currency;
        if (total && typeof total.total_net === 'number') {
          const spanTotal = document.createElement('span');
          spanTotal.textContent = 'Итого: ' +
            total.total_net.toLocaleString('ru-RU', {
              minimumFractionDigits: 2,
              maximumFractionDigits: 2
            }) + ' ' + (total.currency || '');
          meta.appendChild(spanTotal);
        }

        if (plan.request && Array.isArray(plan.request.items)) {
          const spanItems = document.createElement('span');
          spanItems.textContent = 'Позиций: ' + plan.request.items.length;
          meta.appendChild(spanItems);
        }

        bubble.appendChild(meta);

        const details = document.createElement('details');
        const sum = document.createElement('summary');
        sum.textContent = 'Показать JSON-план закупки';
        details.appendChild(sum);

        const pre = document.createElement('pre');
        pre.textContent = JSON.stringify(plan, null, 2);
        details.appendChild(pre);

        bubble.appendChild(details);
      }

      row.appendChild(bubble);
      chat.appendChild(row);
      scrollToBottom();
    }

    async function sendMessage(text) {
      if (!text.trim()) return;
      createBubble('user', text);
      input.value = '';
      setLoading(true);

      try {
        const resp = await fetch('/api/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            message: text,
            conversation_id: conversationId
          })
        });

        if (!resp.ok) {
          const errText = await resp.text();
          createBubble('assistant', 'Ошибка сервера: ' + errText);
          return;
        }

        const data = await resp.json();

        if (data.conversation_id) {
          conversationId = data.conversation_id;
        }

        createBubble('assistant', data.summary, data.plan);
      } catch (err) {
        console.error(err);
        createBubble('assistant', 'Ошибка сети: ' + (err.message || err.toString()));
      } finally {
        setLoading(false);
      }
    }

    form.addEventListener('submit', (e) => {
      e.preventDefault();
      if (isBusy) return;
      const text = input.value.trim();
      if (!text) return;
      sendMessage(text);
    });

    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        if (!isBusy) {
          const text = input.value.trim();
          if (text) sendMessage(text);
        }
      }
    });
  </script>
</body>
</html>
"""


# ----------------- Локальное человекочитаемое резюме плана -----------------


def summarize_plan_for_user(plan: Dict[str, Any], user_message: str) -> str:
    """
    Простое человекочитаемое резюме по JSON-плану.
    Без дополнительного LLM-вызова.
    """
    request = plan.get("request") or {}
    items = request.get("items") or []

    totals_target = plan.get("totals_target_currency") or {}
    totals_supplier = plan.get("totals_supplier_currency") or {}

    # Что показывать в итоге — приоритет у целевой валюты
    display_totals = totals_target or totals_supplier or {}
    currency = str(display_totals.get("currency") or "")
    total_net = display_totals.get("total_net")
    total_items = display_totals.get("total_items")

    supplier_offers = plan.get("supplier_offers")
    unavailable = []
    if isinstance(supplier_offers, dict):
        unavailable = supplier_offers.get("unavailable_skus") or []

    lines: List[str] = []
    lines.append("Вот черновой план закупки по твоему запросу.")

    if isinstance(total_items, int):
        lines.append(f"Всего запрошено товаров: {total_items} шт.")

    if isinstance(total_net, (int, float)):
        lines.append(
            f"Оценочная стоимость закупки: {float(total_net):.2f} {currency or ''}."
        )

    if items:
        lines.append("")
        lines.append("Позиции в плане:")
        for it in items:
            sku = str(it.get("sku") or "позиция без названия")
            qty = it.get("quantity")
            max_price = it.get("max_unit_price")
            if isinstance(qty, int):
                if isinstance(max_price, (int, float)):
                    lines.append(
                        f"- {sku} — {qty} шт., лимит {float(max_price):.2f} за штуку."
                    )
                else:
                    lines.append(f"- {sku} — {qty} шт.")
            else:
                lines.append(f"- {sku}")

    if unavailable:
        lines.append("")
        lines.append(
            "Для следующих позиций не удалось подобрать предложения поставщика:"
        )
        for sku in unavailable:
            lines.append(f"- {sku}")

    return "\n".join(lines)


# ----------------- Маршруты -----------------


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    """Возвращает простую HTML-страницу с чатом."""
    return HTML_PAGE


@app.post("/api/chat", response_model=ChatResponse)
async def chat_endpoint(req: ChatRequest) -> ChatResponse:
    """
    Принимает текст пользователя, строит план закупки через MCP-агента
    и возвращает JSON-план + краткое резюме.

    Поддерживает контекст диалога через conversation_id
    (для отображения истории на фронте).
    """
    logger.info("Incoming chat message: %s", req.message)

    # 1. Определяем / создаём диалог
    conv_id = req.conversation_id
    if not conv_id or conv_id not in conversations:
        conv_id = str(uuid4())
        conversations[conv_id] = []

    history = conversations[conv_id]

    # 2. Строим план (сейчас без передачи history в LLM — запрос обрабатывается как самостоятельный)
    plan = await build_procurement_plan(req.message)

    # 3. Краткое резюме на основе JSON-плана
    summary = summarize_plan_for_user(plan, req.message)

    # 4. Обновляем историю (добавляем текущий обмен)
    history.append({"role": "user", "content": req.message})
    history.append({"role": "assistant", "content": summary})

    conversations[conv_id] = history

    return ChatResponse(
        summary=summary,
        plan=plan,
        conversation_id=conv_id,
    )


def main() -> None:
    import uvicorn

    port = int(os.getenv("WEB_PORT", "8080"))
    uvicorn.run(
        "web_app:app",
        host="0.0.0.0",
        port=port,
        reload=False,
    )


if __name__ == "__main__":
    main()
