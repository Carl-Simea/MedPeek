# MedPeek v1-cloud (2.0): the team build

Describe what's going on or attach an X-ray, MRI or CT scan. MedPeek explains it in plain words, compares the
image with thousands of verified cases, names the kind of doctor who treats it, and, from a ZIP code, lists the
three closest emergency rooms, urgent care centers and specialists on a map.

⚠️ **MedPeek is AI and can make mistakes.** It is a learning project, not a medical device, and not a diagnosis.

## What's in 2.0

- **Chat first.** No forms and no body-part menus: type like you'd talk to a nurse. Gemma 4 answers health
  questions, asks for the scan when it would help, works out the body part itself, and declines unrelated topics.
- **Image reading** against a library of verified reference cases from open, expert-labeled datasets (X-ray, MRI
  and CT; every body part from skull to foot). Every answer names the right kind of doctor and the urgency.
- **Care near you.** Add a ZIP code and MedPeek lists the top 3 emergency rooms (24/7 ERs flagged), urgent care
  centers and the right specialists, with distance, phone number and one-tap directions, on an OpenStreetMap map.
  Built from free US government data only: CMS hospital list, HIFLD hospital locations, the NPI Registry and
  GeoNames ZIP codes. No paid map or API key.
- **Accounts and conversations**, PDF reports to take to your doctor, a "report a problem" button, light and dark
  themes, and a layout that works on phones.

## Layout

```
v1-cloud/
├── backend/    FastAPI API: Gemma 4 chat + image reading, reference library, care finder  (README inside)
└── frontend/   Flask web app: accounts, conversations, chat UI, map, PDF reports
```

## Run it

In the team Codespace (or any machine with Python 3.12 and [uv](https://docs.astral.sh/uv/)):

```bash
# 1. backend (port 8000). Put a Google AI Studio key in backend/.env (see backend/.env.example) for fast answers;
#    without one it falls back to a local Gemma 4 through Ollama.
cd v1-cloud/backend && ./run.sh

# 2. web app (port 5000), in a second terminal
cd v1-cloud/frontend && ./run.sh
```

Open port 5000. For a public demo link, set ports 5000 and 8000 to **Public** in the Codespace's Ports tab.
Tests: `cd v1-cloud/backend && .venv/bin/python -m pytest -q`.

## Reference data

The backend grounds every image reading in verified cases: FracAtlas, OrthoFrac-XR, BTXRD, BDCXR-3257, Indiana
University chest X-rays, ROCOv2 case reports (X-ray, CT and MRI), Mammo-MX mammograms, NIH ChestX-ray14, Kermany
pediatric chest X-rays, GRAZPEDWRI-DX children's wrists, DENTEX and DentIRO dental X-rays, KUB-StoneX kidney stones,
the OAI knee osteoarthritis set, MURA upper-limb studies, PediaSHF-DX elbows, the Cervical Spine X-ray Atlas, SPIDER
lumbar spine MRI and the Cheng et al. brain tumor MRI set. Sizes, licenses, how to download them and what is still
missing are listed in [`backend/README.md`](backend/README.md). The images live in `backend/data/` on the server and
are **never committed** to this repository; neither are patient images, accounts or API keys.

Built at Hacktoberfest Hack Day Brooklyn × GDG on Campus City Tech (October 30, 2026) by
[@Carl-Simea](https://github.com/Carl-Simea), [@Bekefeller](https://github.com/Bekefeller) and
[@sora-moon](https://github.com/sora-moon).
