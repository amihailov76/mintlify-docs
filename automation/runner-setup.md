# Автоматизация документации Doc Reviewer: концепция и настройка

## Как устроена автоматизация

Документационный сайт поддерживается в двух языках: русском (ru/) и английском (en/).
Русский — язык-исходник: все новые страницы и правки пишутся на русском.
Английский генерируется автоматически из русского через LLM.

Автоматизация реализует два независимых сценария.

---

### Сценарий 1. Обновление приложения → обновление документации

Используется, когда в приложении doc-reviewer появилась новая функциональность
и нужно обновить документационный сайт.

**Триггер:** push в ветку `main` репозитория **doc-reviewer**,
затрагивающий отслеживаемые файлы (`README.md`, `docs/`, `criteria.md` и др.).

**Цепочка действий:**

```
push в doc-reviewer/main
        ↓
GitHub Actions (sync-docs.yml в doc-reviewer)
        ↓
Self-hosted runner на вашем компьютере
        ↓
sync_docs.py:
  1. git diff — определяет изменённые файлы
  2. Сопоставляет файлы со страницами документации (file_map в config.yml)
  3. LLM обновляет ru/ страницу на основе diff
  4. LLM переводит обновлённый ru/ в en/
        ↓
PR в mintlify-docs с обновлёнными ru/ и en/ страницами
        ↓
Вы проверяете PR и мёрджите → Mintlify публикует изменения
```

**Что нужно настроить:**
- Self-hosted runner в репозитории **doc-reviewer**
- Секрет `GH_TOKEN` в репозитории **doc-reviewer**
- Файлы автоматизации в `doc-reviewer/docs/automation/`
- Workflow `sync-docs.yml` в `doc-reviewer/.github/workflows/`

---

### Сценарий 2. Правка документации → перевод на английский

Используется, когда вы напрямую редактируете или создаёте страницы
в папке `ru/` репозитория **mintlify-docs**.

**Триггер:** push в ветку `main` репозитория **mintlify-docs**,
затрагивающий файлы в папке `ru/`.

**Цепочка действий:**

```
Вы редактируете ru/ в mintlify-docs и делаете push
        ↓
GitHub Actions (translate-ru-to-en.yml в mintlify-docs)
        ↓
Self-hosted runner на вашем компьютере
        ↓
translate_to_en.py:
  1. git diff — определяет изменённые ru/-файлы
  2. LLM переводит каждый файл в EN
  3. Записывает результат в соответствующие en/-файлы
        ↓
PR в mintlify-docs с обновлёнными en/ страницами
        ↓
Вы проверяете PR и мёрджите → Mintlify публикует изменения
```

**Что нужно настроить:**
- Self-hosted runner в репозитории **mintlify-docs** (отдельная регистрация)
- Секрет `GH_TOKEN` в репозитории **mintlify-docs**
- Workflow `translate-ru-to-en.yml` в `mintlify-docs/.github/workflows/`

---

### Как сценарии взаимодействуют

Оба сценария работают независимо и не мешают друг другу при правильном порядке действий:

| Ситуация | Что делать |
|---|---|
| Новая фича в приложении | Обновите `README.md` или `docs/` в doc-reviewer → сработает сценарий 1 |
| Новый раздел в документации | Создайте файл в `ru/` в mintlify-docs → сработает сценарий 2 |
| Правка существующей ru/-страницы | Редактируйте в mintlify-docs → сработает сценарий 2 |
| Правка существующей en/-страницы | Не редактируйте en/ вручную — он генерируется автоматически |

> **Важно:** файлы в `en/` генерируются автоматически. Не редактируйте их напрямую —
> правки будут перезаписаны при следующем запуске автоматизации.
> Все правки делайте в `ru/`.

---

## Структура файлов автоматизации

```
doc-reviewer/
  docs/
    automation/
      sync_docs.py          ← скрипт сценария 1
      translate_to_en.py    ← скрипт сценария 2
      config.yml            ← общая конфигурация
      style_guide.md        ← стайлгайд (используется в сценарии 1)
  .github/
    workflows/
      sync-docs.yml         ← workflow сценария 1

mintlify-docs/
  .github/
    workflows/
      translate-ru-to-en.yml ← workflow сценария 2
  ru/                       ← язык-исходник, редактируете вручную
  en/                       ← генерируется автоматически
```

---

## Настройка self-hosted runner

Self-hosted runner — агент GitHub Actions, который работает на вашем компьютере
и запускает скрипты локально. Это нужно, чтобы скрипты имели доступ к LLM-моделям.

Для двух сценариев нужно зарегистрировать runner в **двух репозиториях**.
Это два отдельных экземпляра runner в разных папках, но на одном компьютере.

---

### Шаг 1. Установите runner для doc-reviewer

Откройте **PowerShell от имени администратора** и выполните:

```powershell
mkdir C:\actions-runner\doc-reviewer
cd C:\actions-runner\doc-reviewer

# Скачайте runner (используйте актуальную версию из шага регистрации на GitHub)
Invoke-WebRequest -Uri https://github.com/actions/runner/releases/download/v2.317.0/actions-runner-win-x64-2.317.0.zip -OutFile actions-runner.zip
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::ExtractToDirectory("$PWD\actions-runner.zip", "$PWD")
```

Зарегистрируйте runner:

1. Откройте **amihailov76/doc-reviewer → Settings → Actions → Runners → New self-hosted runner**
2. Скопируйте токен регистрации
3. Выполните:

```powershell
.\config.cmd --url https://github.com/amihailov76/doc-reviewer --token ВАШ_ТОКЕН
```

При настройке укажите имя, например `my-pc-doc-reviewer`. Остальное — по умолчанию.

Установите как службу Windows:

```powershell
.\svc.cmd install
.\svc.cmd start
```

---

### Шаг 2. Установите runner для mintlify-docs

```powershell
mkdir C:\actions-runner\mintlify-docs
cd C:\actions-runner\mintlify-docs

Invoke-WebRequest -Uri https://github.com/actions/runner/releases/download/v2.317.0/actions-runner-win-x64-2.317.0.zip -OutFile actions-runner.zip
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::ExtractToDirectory("$PWD\actions-runner.zip", "$PWD")
```

Зарегистрируйте runner:

1. Откройте **amihailov76/mintlify-docs → Settings → Actions → Runners → New self-hosted runner**
2. Скопируйте токен регистрации
3. Выполните:

```powershell
.\config.cmd --url https://github.com/amihailov76/mintlify-docs --token ВАШ_ТОКЕН
```

Имя укажите, например `my-pc-mintlify`. Установите как службу:

```powershell
.\svc.cmd install
.\svc.cmd start
```

---

### Шаг 3. Проверьте, что оба runner видны в GitHub

- **doc-reviewer → Settings → Actions → Runners** — статус **Idle**
- **mintlify-docs → Settings → Actions → Runners** — статус **Idle**

---

### Шаг 4. Добавьте секреты в doc-reviewer

Перейдите в **doc-reviewer → Settings → Secrets and variables → Actions**.

**GH_TOKEN** (обязательно)

Нужен для push в mintlify-docs и создания PR.

1. Создайте Personal Access Token:
   - **GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens**
   - Нажмите **Generate new token**
   - Доступ к репозиториям **doc-reviewer** и **mintlify-docs**:
     - **Contents**: Read and write
     - **Pull requests**: Read and write

2. Добавьте секрет:
   - Name: `GH_TOKEN`, Value: вставьте токен

**LLM_API_KEY** (только если модель требует ключ)

Если вы используете Ollama — этот секрет не нужен, пропустите.

Если используете провайдера с авторизацией:
- Name: `LLM_API_KEY`, Value: ваш API-ключ

> Никогда не вписывайте ключ в `config.yml` — этот файл попадёт в репозиторий.
> Скрипт читает ключ из переменной окружения `LLM_API_KEY`.

---

### Шаг 5. Добавьте секреты в mintlify-docs

Перейдите в **mintlify-docs → Settings → Secrets and variables → Actions**.

Добавьте те же секреты: **GH_TOKEN** и (при необходимости) **LLM_API_KEY**.

---

### Шаг 6. Скопируйте файлы в репозитории

**В doc-reviewer:**

```
docs/
  automation/
    sync_docs.py
    translate_to_en.py
    config.yml
    style_guide.md
.github/
  workflows/
    sync-docs.yml
```

**В mintlify-docs:**

```
.github/
  workflows/
    translate-ru-to-en.yml
```

---

### Шаг 7. Проверьте сценарий 1

Перейдите в **doc-reviewer → Actions → Sync documentation to mintlify-docs**.
Нажмите **Run workflow → Run workflow**.

Следите за логами. При успехе в mintlify-docs появится PR.

---

### Шаг 8. Проверьте сценарий 2

Внесите небольшое изменение в любой файл `ru/` в mintlify-docs и запушьте в main.
Перейдите в **mintlify-docs → Actions → Translate RU to EN**.
При успехе появится PR с обновлённым файлом в `en/`.

---

## Управление runners

| Действие | Команда |
|---|---|
| Запустить doc-reviewer runner | `cd C:\actions-runner\doc-reviewer && .\svc.cmd start` |
| Запустить mintlify runner | `cd C:\actions-runner\mintlify-docs && .\svc.cmd start` |
| Остановить | `.\svc.cmd stop` |
| Статус | `.\svc.cmd status` |

---

## Устранение проблем

**Runner показывает Offline**
Проверьте статус службы: `.\svc.cmd status`. Перезапустите: `.\svc.cmd start`.

**Скрипт не находит LLM**
Убедитесь, что Ollama запущена. Проверьте `api_url` в `config.yml`.
Запустите вручную для диагностики:

```powershell
# Для сценария 1:
$env:LLM_API_KEY = "ваш-ключ"   # только если нужен ключ
python docs/automation/sync_docs.py --dry-run

# Для сценария 2 (из папки mintlify-docs):
python путь/к/automation/translate_to_en.py --dry-run

# Очистите ключ после использования:
Remove-Item Env:LLM_API_KEY
```

**Нет прав на push в mintlify-docs**
Убедитесь, что `GH_TOKEN` в обоих репозиториях имеет доступ на запись к
Contents и Pull requests репозитория mintlify-docs.

**Workflow не триггерится**
Проверьте, что в `paths:` указаны правильные пути. Посмотрите вкладку
**Actions** в репозитории — там видно, какие workflow были запущены и почему.
