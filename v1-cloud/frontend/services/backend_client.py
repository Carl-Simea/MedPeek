"""Server-side client for the team's FastAPI backend."""
import os
import requests


def api_url():
    return os.environ.get('XRAY_BACKEND_URL', 'https://filthy-spooky-mummy-5v4wgppj65j427rpv-8000.app.github.dev').rstrip('/')


def get_options():
    try:
        response = requests.get(api_url() + '/api/options', headers={'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'}, timeout=10)
        if response.status_code in (401, 403):
            return {'body_parts': [], 'modalities': []}, 'The backend requires GitHub access. Ask the backend owner to enable access to forwarded port 8000.'
        response.raise_for_status()
        return response.json(), None
    except (requests.RequestException, ValueError):
        return {'body_parts': [], 'modalities': []}, 'Backend unavailable. Configure XRAY_BACKEND_URL and start the backend.'


def analyze_xray(file, body_part, modality='xray', question=''):
    if not body_part:
        return 'Please select the body part before uploading an image.'
    try:
        response = requests.post(
            api_url() + '/api/analyze', params={'wait': 'true'},
            headers={'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'},
            files={'image': (file.filename, file.stream, file.mimetype)},
            data={'body_part': body_part, 'modality': modality, 'question': question},
            timeout=(10, 1200),
        )
        response.raise_for_status()
        result = response.json()
        return format_result(result)
    except requests.Timeout:
        return 'The backend timed out. Please check its status before trying again.'
    except requests.RequestException:
        return 'The backend could not complete this analysis. Check its connection and server logs.'
    except (ValueError, KeyError, TypeError):
        return 'The backend returned an unexpected response.'



def format_result(result):
    lines = []
    if result.get('mock'):
        lines.append('DEMO RESULT: the backend is using simulated answers.')
    lines.append(result['summary'])
    if result.get('urgency_notice'):
        lines.append(result['urgency_notice'])
    for item in result.get('findings', []):
        lines.append(f"{item['finding']}: {item.get('explanation', '')}")
    if result.get('coverage_note'):
        lines.append(result['coverage_note'])
    if result.get('reference_comparison'):
        lines.append(result['reference_comparison'])
    referral = result.get('referral', {})
    if referral.get('required'):
        lines.append(f"See {referral['specialist']} ({referral['timeframe']}): {referral['reason']}")
    lines.extend(result.get('next_steps', []))
    lines.extend(result.get('safety_notes', []))
    lines.append(result.get('ai_notice', 'MedPeek is AI and can make mistakes.'))
    lines.append(result.get('disclaimer', 'Educational only, not a diagnosis.'))
    return '\n\n'.join(lines)

HEADERS = {'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'}

def start_analysis(file, body_part, modality, question):
    response = requests.post(api_url() + '/api/analyze', headers=HEADERS,
        files={'image': (file.filename, file.stream, file.mimetype)},
        data={'body_part': body_part or 'auto', 'modality': modality or 'xray', 'question': question}, timeout=(10, 60))
    if response.status_code in (400, 413, 422, 429, 503):
        try:
            detail = response.json().get('detail')
        except ValueError:
            detail = None
        raise ValueError(detail if isinstance(detail, str) else 'The backend could not accept this image.')
    response.raise_for_status()
    return response.json()

def poll_analysis(job_id):
    response = requests.get(api_url() + '/api/jobs/' + job_id, headers=HEADERS, timeout=(10, 20))
    response.raise_for_status()
    data = response.json()
    if data.get('status') == 'done':
        data['text'] = format_result(data['result'])
        # Preserve structured fields for the readable result card.
    return data


def chat_turn(messages, zip_code='', report=None):
    """Text-only turn with the MedPeek backend (symptoms, follow-ups, nearby care)."""
    response = requests.post(api_url() + '/api/chat', headers=HEADERS, timeout=(10, 90),
                             json={'messages': messages, 'zip': zip_code or '', 'report': report})
    response.raise_for_status()
    return response.json()


def nearby_care(zip_code, specialist='', urgency='soon', limit=3, include=None):
    """Nearest emergency rooms, urgent care and specialists for a ZIP code (backend care finder)."""
    payload = {'zip': zip_code, 'specialist': specialist or '', 'urgency': urgency or 'soon', 'limit': limit}
    if include:
        payload['include'] = include
    response = requests.post(api_url() + '/api/nearby', headers=HEADERS, timeout=(10, 40), json=payload)
    if response.status_code == 422:
        raise ValueError(response.json().get('detail', 'Please check the ZIP code.'))
    response.raise_for_status()
    return response.json()


def zip_lookup(zip_code):
    response = requests.get(api_url() + '/api/zip/' + str(zip_code)[:5], headers=HEADERS, timeout=(10, 20))
    if response.status_code == 422:
        raise ValueError(response.json().get('detail', 'Please check the ZIP code.'))
    response.raise_for_status()
    return response.json()


def health():
    try:
        response = requests.get(api_url() + '/api/health', headers=HEADERS, timeout=(5, 10))
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError):
        return None
