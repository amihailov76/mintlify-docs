#!/usr/bin/env python3
"""
sync_docs.py — синхронизация документации Doc Reviewer с mintlify-docs.

Определяет изменения в doc-reviewer с момента последнего коммита,
обновляет EN-версию соответствующих страниц и переводит изменения в RU
с применением стайлгайда через локальный LLM API.

Использование:
    python sync_docs.py [--model MODEL] [--since COMMIT] [--dry-run]

Опции:
    --model MODEL     Название модели (переопределяет config.yml)
    --since COMMIT    SHA коммита, с которого читать diff (по умолчанию: HEAD~1)
    --dry-run         Вывести план действий без изменения файлов
    --no-pr           Не создавать PR (только commit + push)
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml

try:
    import httpx
    from openai import OpenAI
except ImportError:
    print("Установите зависимости: pip install openai pyyaml httpx")
    sys.exit(1)


# ─── Загрузка конфигурации ────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.yml"
STYLE_GUIDE_PATH = SCRIPT_DIR / "style_guide.md"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_style_guide() -> str:
    with open(STYLE_GUIDE_PATH, encoding="utf-8") as f:
        return f.read()


# ─── Git-операции ─────────────────────────────────────────────────────────────

def git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} завершился с ошибкой:\n{result.stderr}")
    return (result.stdout or "").strip()


def get_changed_files(repo_path: Path, since: str, watch_paths: list[str]) -> list[str]:
    """Возвращает список файлов, изменённых с указанного коммита."""
    diff_output = git(["diff", "--name-only", since, "HEAD"], cwd=repo_path)
    if not diff_output:
        return []
    changed = diff_output.splitlines()
    # Фильтруем по watch_paths
    result = []
    for f in changed:
        for watch in watch_paths:
            if watch.endswith("/"):
                if f.startswith(watch):
                    result.append(f)
                    break
            else:
                if f == watch:
                    result.append(f)
                    break
    return result


def get_file_content(repo_path: Path, filepath: str, ref: str = "HEAD") -> str:
    """Читает содержимое файла из git-репозитория."""
    try:
        return git(["show", f"{ref}:{filepath}"], cwd=repo_path)
    except RuntimeError:
        return ""


def get_file_diff(repo_path: Path, filepath: str, since: str) -> str:
    """Возвращает diff файла с указанного коммита."""
    try:
        return git(["diff", since, "HEAD", "--", filepath], cwd=repo_path)
    except RuntimeError:
        return ""


# ─── LLM-клиент ───────────────────────────────────────────────────────────────

class LLMClient:
    def __init__(self, cfg: dict, model: str | None = None):
        llm_cfg = cfg["llm"]
        self.model = model or llm_cfg["default_model"]
        self.temperature = llm_cfg.get("temperature", 0.2)
        self.max_tokens = llm_cfg.get("max_tokens", 4096)
        self.timeout = llm_cfg.get("timeout_seconds", 180)

        # API-ключ берём из переменной окружения; config.yml используется как fallback.
        # Для Ollama ключ не нужен — оставьте LLM_API_KEY пустым или не задавайте.
        api_key = (
            os.environ.get("LLM_API_KEY")
            or llm_cfg.get("api_key")
            or "no-key"
        )

        # SSL и прокси:
        # LLM_SSL_VERIFY=false  — отключить проверку сертификата.
        # LLM_CA_BUNDLE=/path  — корпоративный CA-сертификат.
        # LLM_NO_PROXY=true    — обойти системный прокси (по умолчанию: true).
        ca_bundle = os.environ.get("LLM_CA_BUNDLE")
        ssl_verify: bool | str = ca_bundle if ca_bundle else (
            os.environ.get("LLM_SSL_VERIFY", "true").lower() != "false"
        )

        # По умолчанию обходим системный прокси для LLM-эндпоинта —
        # внутренние API обычно доступны напрямую.
        bypass_proxy = os.environ.get("LLM_NO_PROXY", "true").lower() != "false"
        if bypass_proxy:
            transport = httpx.HTTPTransport(verify=ssl_verify)
        else:
            transport = None

        http_client = httpx.Client(verify=ssl_verify, transport=transport)

        self.client = OpenAI(
            base_url=llm_cfg["api_url"],
            api_key=api_key,
            timeout=self.timeout,
            http_client=http_client,
        )

    def complete(self, system: str, user: str) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        content = response.choices[0].message.content.strip()
        return self._strip_code_fence(content)

    @staticmethod
    def _strip_code_fence(content: str) -> str:
        """Убирает код-фенсы, которые LLM иногда добавляет вокруг MDX-контента.

        Обрабатывает два случая:
        1. Весь контент обёрнут в ```...```
        2. Frontmatter снаружи, тело — внутри ```...```
        """
        content = content.strip()

        def remove_fence(text: str) -> str:
            """Снимает один слой код-фенса если он есть."""
            text = text.strip()
            if text.startswith("```"):
                lines = text.splitlines()
                lines = lines[1:]  # убираем открывающую строку
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]  # убираем закрывающую строку
                return "\n".join(lines).strip()
            return text

        # Случай 1: всё содержимое в kod-фенсе
        if content.startswith("```"):
            return remove_fence(content)

        # Случай 2: frontmatter снаружи, тело в kod-фенсе
        if content.startswith("---"):
            fm_end = content.find("---", 3)
            if fm_end != -1:
                frontmatter = content[:fm_end + 3]
                body = content[fm_end + 3:].strip()
                body = remove_fence(body)
                return frontmatter + "\n\n" + body

        return content


# ─── Промпты ──────────────────────────────────────────────────────────────────

SYSTEM_UPDATE_EN = """You are a technical documentation editor for Doc Reviewer.
Your task: update a Mintlify MDX documentation page to reflect changes in the application.
Rules:
- Preserve all MDX components exactly: <Steps>, <Step>, <Card>, <CardGroup>, <Note>, <Warning>, <Tip>, <Accordion>, <AccordionGroup>, <Tabs>, <Tab>, <CodeGroup>, etc.
- Keep frontmatter (---) intact, only update title/description if the change is significant.
- Keep all href links intact, only update them if page names changed.
- Write in clear, neutral technical English. No marketing language.
- Return only the updated MDX content, no explanations."""

SYSTEM_TRANSLATE_RU = """Ты — редактор технической документации Doc Reviewer на русском языке.
Твоя задача: перевести или обновить MDX-страницу документации на русский язык, применяя стайлгайд.

СТАЙЛГАЙД (применяй обязательно):
{style_guide}

ПРАВИЛА ФОРМАТИРОВАНИЯ:
- Сохраняй все MDX-компоненты в точности: <Steps>, <Step>, <Card>, <CardGroup>, <Note>, <Warning>, <Tip>, <Accordion>, <AccordionGroup>, <Tabs>, <Tab>, <CodeGroup> и т.д.
- Frontmatter (---) переводи: title и description — на русский.
- href-ссылки: меняй /en/ на /ru/ там, где они ведут на страницы документации. Внешние URL (github.com, ollama.com и т.д.) не трогай.
- Технические термины оставляй как есть: Doc Reviewer, LLM, API, SQLite, Playwright, PDF, DOCX, Markdown, MDX, XLS.
- Названия элементов интерфейса пиши жирным или в кавычках так же, как в оригинале.
- Возвращай только готовый MDX-контент без пояснений."""


def build_translate_system(style_guide: str) -> str:
    return SYSTEM_TRANSLATE_RU.format(style_guide=style_guide)


# ─── Режим commit-message ─────────────────────────────────────────────────────

SYSTEM_ANALYZE_COMMIT = """You are a documentation maintainer for Doc Reviewer.

Analyze the git diff and commit message. Identify which documentation pages need to be updated.

Available pages:
{pages_list}

Return ONLY a JSON array of page names that need updating.
Example: ["introduction", "quickstart"]
If nothing needs updating, return: []
Return ONLY the JSON array, no explanations, no markdown."""

SYSTEM_UPDATE_EN_FROM_DIFF = """You are a technical documentation editor for Doc Reviewer.
A code change was committed (see diff and commit message below).
Update the documentation page to reflect this change.
Rules:
- Preserve all MDX components exactly: <Steps>, <Step>, <Card>, <CardGroup>, <Note>, <Warning>, <Tip>, etc.
- Keep frontmatter (---) intact unless title/description should meaningfully change.
- Keep all href links intact.
- Write clear, neutral technical English. No marketing language.
- Only update what is actually affected by the change.
- Return only the updated MDX content, no explanations."""

SYSTEM_UPDATE_RU_FROM_DIFF = """Ты — редактор технической документации Doc Reviewer на русском языке.
Был сделан коммит в коде приложения (см. diff и commit message ниже).
Обнови MDX-страницу документации на русском языке, чтобы отразить это изменение.

СТАЙЛГАЙД (применяй обязательно):
{{style_guide}}

ПРАВИЛА:
- Сохраняй все MDX-компоненты в точности.
- Frontmatter (---) переводи: title и description — на русский.
- Технические термины оставляй как есть: Doc Reviewer, LLM, API, SQLite, Playwright, PDF, DOCX, MDX.
- Меняй только то, что затронуто изменением.
- Возвращай только готовый MDX-контент без пояснений."""


def get_full_diff(repo_path: Path, since: str) -> str:
    """Возвращает полный diff репозитория с указанного коммита."""
    try:
        return git(["diff", since, "HEAD"], cwd=repo_path)
    except RuntimeError:
        return ""


def get_commit_message(repo_path: Path) -> str:
    """Возвращает сообщение последнего коммита."""
    try:
        return git(["log", "-1", "--format=%s%n%b"], cwd=repo_path)
    except RuntimeError:
        return ""


def get_available_pages(mintlify_docs_path: Path) -> list[str]:
    """Возвращает список доступных страниц из папки ru/."""
    ru_path = mintlify_docs_path / "ru"
    if not ru_path.exists():
        return []
    pages = []
    for mdx_file in sorted(ru_path.rglob("*.mdx")):
        rel = mdx_file.relative_to(ru_path)
        page = str(rel.with_suffix("")).replace("\\", "/")
        pages.append(page)
    return pages


def analyze_commit_for_pages(
    llm: LLMClient,
    diff: str,
    commit_msg: str,
    available_pages: list[str],
) -> list[str]:
    """Спрашивает LLM, какие страницы документации затронуты коммитом."""
    pages_list = "\n".join(f"- {p}" for p in available_pages)
    system = SYSTEM_ANALYZE_COMMIT.format(pages_list=pages_list)
    # Ограничиваем diff чтобы не превысить контекст модели
    diff_snippet = diff[:6000] + ("\n... (diff truncated)" if len(diff) > 6000 else "")
    user = f"Commit message:\n{commit_msg}\n\nGit diff:\n```\n{diff_snippet}\n```"

    response = llm.complete(system, user).strip()
    # Снимаем код-фенс если LLM добавил
    if response.startswith("```"):
        lines = response.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        response = "\n".join(lines).strip()
    try:
        pages = json.loads(response)
        valid = [p for p in pages if p in available_pages]
        return valid
    except (json.JSONDecodeError, TypeError):
        print(f"  ⚠ Не удалось разобрать ответ LLM: {response[:300]}")
        return []


def update_en_from_diff(
    llm: LLMClient,
    current_en: str,
    diff: str,
    commit_msg: str,
) -> str:
    """Обновляет EN-страницу на основе полного diff коммита."""
    diff_snippet = diff[:4000] + ("\n... (truncated)" if len(diff) > 4000 else "")
    user = (
        f"Commit message:\n{commit_msg}\n\n"
        f"Git diff:\n```diff\n{diff_snippet}\n```\n\n"
        f"Current documentation page (EN):\n```mdx\n{current_en}\n```\n\n"
        "Update the page to reflect the changes from the commit."
    )
    return llm.complete(SYSTEM_UPDATE_EN_FROM_DIFF, user)


def update_ru_from_diff(
    llm: LLMClient,
    current_ru: str,
    updated_en: str,
    style_guide: str,
) -> str:
    """Обновляет RU-страницу на основе актуальной EN-версии."""
    system = SYSTEM_UPDATE_RU_FROM_DIFF.replace("{{style_guide}}", style_guide)
    user = (
        f"EN-версия страницы (актуальная):\n```mdx\n{updated_en}\n```\n\n"
        f"RU-версия страницы (текущая):\n```mdx\n{current_ru}\n```\n\n"
        "Обнови RU-версию, чтобы она соответствовала EN-версии."
    )
    return llm.complete(system, user)


# ─── Основная логика синхронизации ────────────────────────────────────────────

def map_changed_files_to_pages(changed_files: list[str], file_map: dict) -> dict[str, list[str]]:
    """Соотносит изменённые файлы с страницами mintlify-docs."""
    result = {}
    for filepath in changed_files:
        if filepath in file_map:
            pages = file_map[filepath]
            result[filepath] = pages if isinstance(pages, list) else [pages]
    return result


def update_en_page(
    llm: LLMClient,
    current_mdx: str,
    source_file: str,
    source_content: str,
    source_diff: str,
) -> str:
    """Обновляет EN-версию страницы на основе изменений в исходном файле."""
    user_prompt = f"""Исходный файл приложения, который изменился: {source_file}

DIFF изменений:
```diff
{source_diff}
```

ПОЛНОЕ СОДЕРЖИМОЕ исходного файла (актуальное):
```
{source_content}
```

ТЕКУЩАЯ MDX-страница документации (EN):
```mdx
{current_mdx}
```

Обнови MDX-страницу документации, чтобы отразить изменения из diff.
Если изменения в исходном файле не влияют на содержимое страницы, верни страницу без изменений."""
    return llm.complete(SYSTEM_UPDATE_EN, user_prompt)


def translate_to_ru(
    llm: LLMClient,
    en_mdx: str,
    ru_mdx: str,
    style_guide: str,
) -> str:
    """Переводит/обновляет RU-версию страницы на основе актуальной EN-версии."""
    system = build_translate_system(style_guide)
    if ru_mdx:
        user_prompt = f"""EN-версия страницы (актуальная, после обновления):
```mdx
{en_mdx}
```

RU-версия страницы (текущая, требует обновления):
```mdx
{ru_mdx}
```

Обнови RU-версию, чтобы она соответствовала актуальной EN-версии, применяя стайлгайд.
Меняй только то, что изменилось в EN-версии. Сохраняй стиль уже переведённых фрагментов."""
    else:
        user_prompt = f"""EN-версия страницы (для перевода):
```mdx
{en_mdx}
```

Переведи страницу на русский язык, применяя стайлгайд."""
    return llm.complete(system, user_prompt)


# ─── Работа с файлами ─────────────────────────────────────────────────────────

def read_mdx(docs_path: Path, lang: str, page: str) -> str:
    filepath = docs_path / lang / f"{page}.mdx"
    if filepath.exists():
        return filepath.read_text(encoding="utf-8")
    return ""


def write_mdx(docs_path: Path, lang: str, page: str, content: str, dry_run: bool):
    filepath = docs_path / lang / f"{page}.mdx"
    filepath.parent.mkdir(parents=True, exist_ok=True)
    if dry_run:
        print(f"  [dry-run] Записал бы: {filepath}")
    else:
        filepath.write_text(content, encoding="utf-8")
        print(f"  ✓ Обновлено: {filepath}")


# ─── Git-workflow для mintlify-docs ───────────────────────────────────────────

def create_sync_branch(docs_path: Path, branch_prefix: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M")
    branch = f"{branch_prefix}/{timestamp}"
    git(["checkout", "-b", branch], cwd=docs_path)
    return branch


def commit_and_push(docs_path: Path, message: str, author_name: str, author_email: str):
    git(["add", "-A"], cwd=docs_path)
    git(
        [
            "-c", f"user.name={author_name}",
            "-c", f"user.email={author_email}",
            "commit", "-m", message,
        ],
        cwd=docs_path,
    )
    branch = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=docs_path)
    git(["push", "-u", "origin", branch], cwd=docs_path)
    return branch


def create_pr(docs_path: Path, branch: str, base: str, title: str, body: str):
    try:
        subprocess.run(
            ["gh", "pr", "create",
             "--title", title,
             "--body", body,
             "--base", base,
             "--head", branch],
            cwd=docs_path,
            check=True,
        )
        print("  ✓ PR создан")
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("  ⚠ Не удалось создать PR автоматически. Создайте PR вручную.")


# ─── Точка входа ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Синхронизация документации Doc Reviewer")
    parser.add_argument("--model", help="Название модели LLM")
    parser.add_argument("--since", default="HEAD~1", help="Коммит, с которого читать diff")
    parser.add_argument("--dry-run", action="store_true", help="Не менять файлы")
    parser.add_argument("--no-pr", action="store_true", help="Не создавать PR")
    parser.add_argument("--commit-mode", action="store_true",
                        help="Режим commit-message: LLM сам определяет страницы по diff")
    args = parser.parse_args()

    cfg = load_config()
    style_guide = load_style_guide()

    doc_reviewer_path = Path(cfg["repos"]["doc_reviewer"]).resolve()
    mintlify_docs_path = Path(cfg["repos"]["mintlify_docs"]).resolve()

    print(f"📖 Репозиторий приложения: {doc_reviewer_path}")
    print(f"📄 Репозиторий документации: {mintlify_docs_path}")

    # ── Режим commit-message ──────────────────────────────────────────────────
    if args.commit_mode:
        print(f"🔖 Режим: commit-message (LLM определяет страницы по diff)\n")
        commit_msg = get_commit_message(doc_reviewer_path)
        print(f"📝 Коммит: {commit_msg.splitlines()[0][:100]}")

        full_diff = get_full_diff(doc_reviewer_path, args.since)
        if not full_diff:
            print("✅ Нет изменений в репозитории.")
            return

        available_pages = get_available_pages(mintlify_docs_path)
        print(f"📚 Доступных страниц документации: {len(available_pages)}")

        llm = LLMClient(cfg, args.model)
        print(f"🤖 Используется модель: {llm.model}\n")

        print("🔍 Анализирую diff — LLM определяет затронутые страницы...")
        pages_to_update = analyze_commit_for_pages(llm, full_diff, commit_msg, available_pages)

        if not pages_to_update:
            print("✅ LLM решил, что документация не требует обновления.")
            return

        print(f"\n📑 Страницы для обновления: {', '.join(pages_to_update)}")

        if args.dry_run:
            print("\n[dry-run] Остановка перед изменением файлов.")
            return

        git_cfg = cfg["git"]
        branch = create_sync_branch(mintlify_docs_path, git_cfg["branch_prefix"])
        print(f"🌿 Создана ветка: {branch}\n")

        updated_pages = []
        for page in pages_to_update:
            print(f"⚙️  Обрабатываю страницу: {page}")

            print(f"  → Обновление EN...")
            current_en = read_mdx(mintlify_docs_path, "en", page)
            updated_en = update_en_from_diff(llm, current_en, full_diff, commit_msg)
            write_mdx(mintlify_docs_path, "en", page, updated_en, dry_run=False)

            print(f"  → Обновление RU...")
            current_ru = read_mdx(mintlify_docs_path, "ru", page)
            updated_ru = update_ru_from_diff(llm, current_ru, updated_en, style_guide)
            write_mdx(mintlify_docs_path, "ru", page, updated_ru, dry_run=False)

            updated_pages.append(page)

        print(f"\n📤 Коммит и пуш...")
        pushed_branch = commit_and_push(
            mintlify_docs_path,
            f"docs: sync {len(updated_pages)} page(s) from commit\n\n"
            f"Updated: {', '.join(updated_pages)}\n"
            f"Triggered by: {commit_msg.splitlines()[0][:80]}\n"
            f"Model: {llm.model}",
            git_cfg["commit_author_name"],
            git_cfg["commit_author_email"],
        )
        print(f"  ✓ Запушено в {pushed_branch}")

        if not args.no_pr:
            print(f"\n🔀 Создаю PR...")
            create_pr(
                mintlify_docs_path,
                pushed_branch,
                git_cfg["pr_base_branch"],
                f"docs: sync {len(updated_pages)} page(s) from doc-reviewer",
                f"## Обновление документации\n\n"
                f"**Коммит:** {commit_msg.splitlines()[0]}\n\n"
                f"**Страницы:** {', '.join(f'`{p}`' for p in updated_pages)}\n\n"
                f"**Модель:** {llm.model}\n\n"
                "*Создано автоматически по тегу `[docs]` в commit message*",
            )

        print(f"\n✅ Готово. Обновлено страниц: {len(updated_pages)}")
        return
    # ── Конец режима commit-message ───────────────────────────────────────────

    print(f"🔍 Анализирую изменения с {args.since}...")

    # 1. Определяем изменённые файлы
    changed = get_changed_files(
        doc_reviewer_path,
        args.since,
        cfg["sync"]["watch_paths"],
    )
    if not changed:
        print("✅ Нет изменений в отслеживаемых файлах. Синхронизация не требуется.")
        return

    print(f"\n📝 Изменённые файлы ({len(changed)}):")
    for f in changed:
        print(f"  - {f}")

    # 2. Сопоставляем с страницами документации
    pages_to_update = map_changed_files_to_pages(changed, cfg["sync"]["file_map"])
    if not pages_to_update:
        print("✅ Изменения не затрагивают отображённые страницы документации.")
        return

    print(f"\n📑 Страницы для обновления:")
    for src, pages in pages_to_update.items():
        print(f"  {src} → {', '.join(pages)}")

    if args.dry_run:
        print("\n[dry-run] Остановка перед изменением файлов.")
        return

    # 3. Инициализируем LLM-клиент
    llm = LLMClient(cfg, args.model)
    print(f"\n🤖 Используется модель: {llm.model}")

    # 4. Создаём ветку в mintlify-docs
    git_cfg = cfg["git"]
    branch = create_sync_branch(mintlify_docs_path, git_cfg["branch_prefix"])
    print(f"\n🌿 Создана ветка: {branch}")

    updated_pages = []

    for source_file, pages in pages_to_update.items():
        source_content = get_file_content(doc_reviewer_path, source_file)
        source_diff = get_file_diff(doc_reviewer_path, source_file, args.since)

        for page in pages:
            print(f"\n⚙️  Обрабатываю страницу: {page}")

            # 4a. Обновляем EN
            print(f"  → Обновление EN...")
            current_en = read_mdx(mintlify_docs_path, "en", page)
            updated_en = update_en_page(llm, current_en, source_file, source_content, source_diff)
            write_mdx(mintlify_docs_path, "en", page, updated_en, dry_run=False)

            # 4b. Обновляем RU
            print(f"  → Перевод/обновление RU...")
            current_ru = read_mdx(mintlify_docs_path, "ru", page)
            updated_ru = translate_to_ru(llm, updated_en, current_ru, style_guide)
            write_mdx(mintlify_docs_path, "ru", page, updated_ru, dry_run=False)

            updated_pages.append(page)

    # 5. Коммит и пуш
    print(f"\n📤 Коммит и пуш в ветку {branch}...")
    pages_list = ", ".join(updated_pages)
    commit_msg = f"docs: sync {len(updated_pages)} page(s) from doc-reviewer\n\nUpdated: {pages_list}\nModel: {llm.model}\nSource commit: {args.since}"
    pushed_branch = commit_and_push(
        mintlify_docs_path,
        commit_msg,
        git_cfg["commit_author_name"],
        git_cfg["commit_author_email"],
    )
    print(f"  ✓ Запушено в {pushed_branch}")

    # 6. Создаём PR
    if git_cfg.get("create_pr") and not args.no_pr:
        print(f"\n🔀 Создаю PR...")
        pr_body = f"""## Автоматическое обновление документации

Обновлены страницы:
{chr(10).join(f'- `{p}`' for p in updated_pages)}

**Источник изменений:** {', '.join(pages_to_update.keys())}
**Модель:** {llm.model}
**Коммит doc-reviewer:** `{args.since}`

*Создано автоматически скриптом sync_docs.py*"""
        create_pr(
            mintlify_docs_path,
            pushed_branch,
            git_cfg["pr_base_branch"],
            f"docs: sync {len(updated_pages)} page(s) from doc-reviewer",
            pr_body,
        )

    print(f"\n✅ Синхронизация завершена. Обновлено страниц: {len(updated_pages)}")


if __name__ == "__main__":
    main()
