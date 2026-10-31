# 🩻 MedPeek

**Peek inside the break.** Upload a medical image, and Gemma 4 explains in plain language what it sees. MedPeek comes in two versions. The cloud version reads X-rays, MRIs and CT scans of every body part. The local version is a lightweight prototype that runs on your own computer and focuses on hand X-rays, including children's hands.

> **In honor of Hacktoberfest Hack Day Brooklyn × GDG on Campus City Tech**
> October 30, 2026 · New York City College of Technology (City Tech), 285 Jay St, Brooklyn
>
> MedPeek started at my first hackathon. The Gemma 4 workshop at Hack Day Brooklyn showed me that a capable vision model can run on an ordinary laptop, and I wanted to try it on something that matters: helping people understand their own X-rays. Thank you to Ousmane Diallo and the GDG on Campus City Tech team, Major League Hacking, and Hacktoberfest for creating the space to build it.

⚠️ **MedPeek is a learning project, not a medical device.** It can be wrong. It does not diagnose anything. Always have a doctor read your scans.

---

## Two versions

| | **v1-cloud** | **v2-local** |
|---|---|---|
| What it is | The team build from Hack Day | My own version that runs fully offline |
| Where Gemma 4 runs | A FastAPI backend in a Codespace or on Google AI Studio | On your own computer through Ollama |
| Body parts | Every body part, from skull to foot | Hands, including children's hands |
| Image types | X-ray, MRI and CT scan | X-ray |
| Filters | Choose the body part and image type when you upload | None needed (hands only) |
| Interface | Upload form plus results page | Chat with history and an ocean-teal theme |
| Folder | [`v1-cloud/`](v1-cloud/) | [`v2-local/`](v2-local/) |

### v1-cloud: the team build
Because Gemma 4 runs in the cloud, this version has room for a large reference library covering the whole body.

- **Filter options when you upload:** pick the **body part** (skull and face, teeth and jaw, neck, chest for adults or children, breast, back, abdomen and kidneys, pelvis and hip, shoulder, elbow and forearm, wrist and hand, thigh and knee, lower leg and foot, or whole body) and the **image type** (X-ray, MRI or CT scan).
- A FastAPI backend (`v1-cloud/backend`) cleans each upload, removes names and other personal details, finds similar verified reference cases for that body part and asks Gemma 4 to explain the image.
- Safety rules on the server make sure every answer names the kind of doctor to see, and emergencies point to the emergency room.
- A Flask frontend (`v1-cloud/frontend`) handles accounts and conversations.

See [`v1-cloud/README.md`](v1-cloud/README.md).

Built with teammates [@Bekefeller](https://github.com/Bekefeller) and [@sora-moon](https://github.com/sora-moon).

### v2-local: my own version
A single Flask app that needs no cloud and no API key. Everything, including the AI, runs on your own computer.

**Why hands only?** v2-local is a prototype. Running locally means every reference X-ray has to live in a database on your own computer, so I kept it small to save storage, with 20 normal hand X-rays. Hands were a good place to start because they have many small bones and both adult and child cases. Covering the whole body would need a much bigger reference library and more testing than one hackathon day allowed. That's the job the cloud version does.

- **Sign in** with your first name. MedPeek greets you and replies by name.
- **Chat history**, with each conversation saved to your account so you can come back to it.
- **Drag, drop or paste** up to 5 X-rays at a time.
- **Compares against a normal hand.** Gemma 4 sees a healthy reference X-ray beside yours and lists each break: the bone, where it is, the type of break, whether it is displaced, and a likely cause.
- **Spots children's hands** from open growth plates, and avoids mistaking them for breaks.
- **Starts Gemma on its own.** On launch it runs `ollama serve`, pulls `gemma4` if needed and loads the model, with a live status pill in the corner.
- A responsive design that works on phones, with light and dark modes.

**Stack:** Python · Flask · SQLite · Ollama · Gemma 4 · Bootstrap 5 · vanilla JavaScript

---

## Run v2-local on your computer

You need Python 3.10 or newer, [Ollama](https://ollama.com) and about 16 GB of RAM (32 GB is comfortable).

```bash
git clone https://github.com/Carl-Simea/MedPeek.git
cd MedPeek/v2-local
python -m pip install -r requirements.txt
python create_db.py      # one time only: downloads FracAtlas and saves 20 normal hand references
python app.py            # starts Ollama and Gemma 4, then the website
```

Open **http://127.0.0.1:5050**, create an account and drop in a hand X-ray. The first start takes a few minutes while Gemma 4 downloads and loads.

To use another port, set `PORT` first: `PORT=8080 python app.py` (macOS/Linux) or `$env:PORT=8080; python app.py` (Windows PowerShell). Chrome blocks some ports, such as 5060, so use 8080, 8000 or 3000 instead.

| Setting | Default | What it does |
|---|---|---|
| `PORT` | `5050` | Website port |
| `MEDPEEK_MODEL` | `gemma4` | Ollama model tag, for example `gemma4:12b` |
| `OLLAMA_URL` | `http://localhost:11434` | Where Ollama is listening |
| `MEDPEEK_DB` | `v2-local/images.db` | Database file |

Your account, chats and uploads stay in `images.db` on your computer. It is git-ignored and never pushed.

---

## Project layout

```
MedPeek/
├── README.md
├── v1-cloud/
│   ├── README.md
│   ├── backend/            FastAPI + Gemma 4 (Codespace or Google AI Studio)
│   └── frontend/           Flask frontend for the backend
└── v2-local/
    ├── app.py              routes, login, chat API
    ├── create_db.py        builds images.db from FracAtlas
    ├── services/
    │   ├── db.py           users, chats, messages, reference images
    │   ├── analyzer.py     Gemma 4 prompt and structured answer
    │   └── ollama_client.py  starts Ollama, pulls and warms up Gemma 4
    ├── templates/          base, home (sign in), results (chat), _messages
    └── static/             style.css, script.js
```

---

## Data and credits

- Normal hand reference images come from **FracAtlas** (Abedeen et al., 2023, *Scientific Data*), licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). `create_db.py` downloads them from [figshare](https://doi.org/10.6084/m9.figshare.22363012). No medical images are stored in this repository.
- **Gemma 4** by Google DeepMind, served locally by **Ollama**.
- Thanks to GDG on Campus City Tech, Major League Hacking and Hacktoberfest for Hack Day Brooklyn.

## Limits

- Gemma 4 is a general vision model and was not trained to read X-rays. Its answers can be confidently wrong, so treat them as a starting point for a conversation with a doctor.
- v2-local reads hand X-rays only. For other body parts, MRIs or CT scans, use v1-cloud.
- Built in one day at a hackathon. Not tested clinically.