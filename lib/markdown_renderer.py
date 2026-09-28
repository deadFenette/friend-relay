"""
Markdown-рендеринг для чата — простой парсер подмножества Markdown.

Поддерживает:
  - **bold**
  - *italic*
  - `inline code`
  - ```code blocks```
  - > blockquote
  - [text](url) links
  - ссылки https://... (кликабельные)
  - экранирование HTML (защита от XSS — <> и & превращаются в сущности)

Не использует сторонних зависимостей — обычные regex'ы. Это намеренно:
  - markdown lib весит 1+ МБ, мы хотим лёгкий проект
  - нам нужен ограниченный набор (не полный CommonMark)
  - результат рендерится в QLabel через HTML (Qt rich text), который
    поддерживает только базовые теги

Результат безопасен для отображения — все HTML-символы в исходном тексте
экранируются ДО применения markdown-разметки.
"""
from __future__ import annotations

import html
import re

# Порядок важен: сначала code blocks (чтобы не парсить markdown внутри),
# потом inline code, потом blockquote, потом bold, italic, links.

def render_markdown(text: str) -> str:
    """Преобразует markdown-текст в HTML, безопасный для QLabel.

    Все HTML-символы в исходном тексте экранируются — это защита от XSS.
    Markdown-разметка применяется после экранирования.
    """
    if not text:
        return ""

    # 1. Экранируем HTML — защита от XSS
    text = html.escape(text, quote=True)

    # 2. Сохраняем code blocks (```...```) — внутри них НЕТ markdown
    code_blocks: list[str] = []
    def _save_code_block(m: re.Match) -> str:
        code = m.group(1).rstrip()
        code_blocks.append(code)
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"  # placeholder
    text = re.sub(r"```(?:[^\n]*)?\n(.*?)```", _save_code_block, text, flags=re.DOTALL)

    # 3. Разбиваем по строкам — для blockquote и paragraphs
    lines = text.split("\n")
    result_lines: list[str] = []
    in_blockquote = False
    paragraph: list[str] = []

    def flush_paragraph():
        nonlocal paragraph
        if paragraph:
            p = "<br>".join(paragraph)
            result_lines.append(f"<p>{p}</p>")
            paragraph = []

    for line in lines:
        # Blockquote: > text
        bq_match = re.match(r"^\s*&gt;\s?(.*)$", line)
        if bq_match:
            if not in_blockquote:
                flush_paragraph()
                in_blockquote = True
            paragraph.append(f"<i>│</i> {bq_match.group(1)}")
            continue
        elif in_blockquote:
            # Закрыли blockquote
            flush_paragraph()
            in_blockquote = False

        if line.strip() == "":
            # Пустая строка — разделитель параграфов
            flush_paragraph()
        else:
            paragraph.append(line)

    flush_paragraph()
    text = "".join(result_lines) if result_lines else "<br>".join(lines)

    # 4. Inline code: `code`
    text = re.sub(
        r"`([^`\n]+)`",
        lambda m: f'<code style="background:rgba(255,255,255,0.08);'
                  f'padding:1px 4px;border-radius:3px;'
                  f'font-family:Consolas,monospace;">{m.group(1)}</code>',
        text,
    )

    # 5. Bold: **text** (должен идти раньше italic, иначе * съест **)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)

    # 6. Italic: *text*
    text = re.sub(r"\*([^*]+)\*", r"<i>\1</i>", text)

    # 7. Links: [text](url)
    def _make_link(m: re.Match) -> str:
        link_text = m.group(1)
        url = m.group(2)
        # v3: разрешаем http(s) и наш собственный deep-link friendrelay://
        # (для кликабельных приглашений в игры — см. lib/bots/chess.py).
        # javascript: и прочее — отбрасываем (защита от XSS).
        if not (url.startswith(("http://", "https://", "friendrelay://"))):
            return m.group(0)
        return f'<a href="{url}" style="color:#7C6BFF;">{link_text}</a>'
    text = re.sub(r"\[([^\]]+)\]\(([^\)]+)\)", _make_link, text)

    # 8. Bare URLs: https://example.com → ссылка
    # (только если ещё не внутри <a href>)
    # v3: также bare friendrelay:// — чтобы вставленный кодом линк
    # тоже стал кликабельным без markdown-обёртки.
    url_pattern = r'(?<!href=")(?<!href=\')(?:https?://[^\s<>"\')]+|friendrelay://[^\s<>"\')]+)'
    def _make_bare_url(m: re.Match) -> str:
        url = m.group(0)
        # Обрезаем trailing punctuation
        while url and url[-1] in ".,;:!?":
            url = url[:-1]
        display = url if len(url) <= 50 else url[:47] + "..."
        return f'<a href="{url}" style="color:#7C6BFF;">{display}</a>'
    text = re.sub(url_pattern, _make_bare_url, text)

    # 9. Восстанавливаем code blocks
    for i, code in enumerate(code_blocks):
        escaped = code  # уже экранирован на шаге 1
        # Каждую строку оборачиваем, чтобы было похоже на блок
        code_html = (
            f'<pre style="background:rgba(255,255,255,0.06);'
            f'padding:8px 12px;border-radius:6px;'
            f'font-family:Consolas,monospace;font-size:12px;'
            f'white-space:pre-wrap;">{escaped}</pre>'
        )
        text = text.replace(f"\x00CODEBLOCK{i}\x00", code_html)

    # 10. <p>...</p> слипаются без пробела — добавляем между ними
    text = text.replace("</p><p>", "</p><br><p>")

    return text


def has_markdown(text: str) -> bool:
    """Проверяет, есть ли в тексте markdown-разметка.
    Используется чтобы не гонять парсер на plain text.
    """
    if not text:
        return False
    indicators = ["**", "*", "`", "```", ">", "http://", "https://", "["]
    return any(ind in text for ind in indicators)
