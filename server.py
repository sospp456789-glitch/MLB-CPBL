from flask import Flask, jsonify, render_template, request, abort
import os
import requests
from bs4 import BeautifulSoup
import threading
import time
import json
import re
from datetime import datetime, timezone, timedelta
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, PushMessageRequest, ReplyMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.exceptions import InvalidSignatureError

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True

TW = timezone(timedelta(hours=8))
ET = timezone(timedelta(hours=-4))   # US Eastern (EDT)

def now_tw():
    """Current datetime in Taiwan time (UTC+8)."""
    return datetime.now(tz=TW)

def now_et():
    """Current datetime in US Eastern time (EDT, UTC-4) — used for MLB date queries."""
    return datetime.now(tz=ET)

# ── LINE 設定 ─────────────────────────────────────────────────────────────────
LINE_SECRET = os.environ.get('LINE_SECRET', '46daa5248c461e26c987c9803635c2e0')
LINE_TOKEN  = os.environ.get('LINE_TOKEN',  '9X2wPChNY6eiNhnHjUSYO94+F+yQaNPaZxOkPcBHd+qD9o7srsFTTWY2QGKuEUutOeogLikgmaOQzpsgalAix3+NFD5O79gOaFXtXyqHLSqPrzRhK0tmFEkwNoUZjCUIG+um3789ox9hVk6Ukti90gdB04t89/1O/w1cDnyilFU=')
line_handler = WebhookHandler(LINE_SECRET)
line_config  = Configuration(access_token=LINE_TOKEN)

USER_IDS_FILE = '/data/line_user_ids.json'

def load_user_ids():
    try:
        with open(USER_IDS_FILE, 'r') as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_user_ids():
    try:
        with open(USER_IDS_FILE, 'w') as f:
            json.dump(list(line_user_ids), f)
    except Exception as e:
        print(f'[LINE] save_user_ids error: {e}')

line_user_ids = load_user_ids()   # 從檔案載入，重啟後不會遺失
prev_game_states = {}   # 追蹤比賽狀態變化

def send_line(msg):
    """推播訊息給所有已加好友的用戶"""
    if not line_user_ids:
        return
    with ApiClient(line_config) as api_client:
        api = MessagingApi(api_client)
        for uid in line_user_ids:
            try:
                api.push_message(PushMessageRequest(
                    to=uid,
                    messages=[TextMessage(text=msg)]
                ))
            except Exception as e:
                print(f"[LINE] push error: {e}")

data_store = {
    'standings': [],
    'pitching': [],
    'batting': [],
    'fielding': [],
    'games': [],          # today's games
    'tomorrow_games': [],   # tomorrow's games
    'yesterday_games': [],  # yesterday's games
    'last_updated': None,
    'games_updated': None,
    'error': None,
    'year': str(now_tw().year),
    'season': '1',
}
data_lock = threading.Lock()

# ── MLB ───────────────────────────────────────────────────────────────────────
mlb_store = {
    'standings': [],   # list of divisions, each with teams
    'games':     [],   # today's games
    'last_updated':  None,
    'games_updated': None,
    'error': None,
}
mlb_lock = threading.Lock()

_ESPN = 'https://a.espncdn.com/i/teamlogos/mlb/500'

# MLB team ID → ESPN logo abbreviation (IDs from MLB Stats API)
_MLB_ID_LOGO = {
    108: 'laa', 109: 'ari', 110: 'bal', 111: 'bos',
    112: 'chc', 113: 'cin', 114: 'cle', 115: 'col',
    116: 'det', 117: 'hou', 118: 'kc',  119: 'lad',
    120: 'wsh', 121: 'nym', 133: 'oak', 134: 'pit',
    135: 'sd',  136: 'sea', 137: 'sf',  138: 'stl',
    139: 'tb',  140: 'tex', 141: 'tor', 142: 'min',
    143: 'phi', 144: 'atl', 145: 'chw', 146: 'mia',
    147: 'nyy', 158: 'mil',
}

def _logo_url(team_id):
    abbr = _MLB_ID_LOGO.get(team_id)
    return f'{_ESPN}/{abbr}.png' if abbr else ''

MLB_TEAM_COLORS = {
    'New York Yankees':       {'color': '#003087', 'accent': '#C4CED4', 'logo': f'{_ESPN}/nyy.png'},
    'Toronto Blue Jays':      {'color': '#134A8E', 'accent': '#E8291C', 'logo': f'{_ESPN}/tor.png'},
    'Baltimore Orioles':      {'color': '#DF4601', 'accent': '#000000', 'logo': f'{_ESPN}/bal.png'},
    'Boston Red Sox':         {'color': '#BD3039', 'accent': '#0C2340', 'logo': f'{_ESPN}/bos.png'},
    'Tampa Bay Rays':         {'color': '#092C5C', 'accent': '#8FBCE6', 'logo': f'{_ESPN}/tb.png'},
    'Chicago White Sox':      {'color': '#27251F', 'accent': '#C4CED4', 'logo': f'{_ESPN}/chw.png'},
    'Cleveland Guardians':    {'color': '#00385D', 'accent': '#E31937', 'logo': f'{_ESPN}/cle.png'},
    'Detroit Tigers':         {'color': '#0C2340', 'accent': '#FA4616', 'logo': f'{_ESPN}/det.png'},
    'Kansas City Royals':     {'color': '#004687', 'accent': '#C09A5B', 'logo': f'{_ESPN}/kc.png'},
    'Minnesota Twins':        {'color': '#002B5C', 'accent': '#D31145', 'logo': f'{_ESPN}/min.png'},
    'Houston Astros':         {'color': '#002D62', 'accent': '#EB6E1F', 'logo': f'{_ESPN}/hou.png'},
    'Los Angeles Angels':     {'color': '#003263', 'accent': '#BA0021', 'logo': f'{_ESPN}/laa.png'},
    'Oakland Athletics':      {'color': '#003831', 'accent': '#EFB21E', 'logo': f'{_ESPN}/oak.png'},
    'Seattle Mariners':       {'color': '#0C2C56', 'accent': '#005C5C', 'logo': f'{_ESPN}/sea.png'},
    'Texas Rangers':          {'color': '#003278', 'accent': '#C0111F', 'logo': f'{_ESPN}/tex.png'},
    'Atlanta Braves':         {'color': '#CE1141', 'accent': '#13274F', 'logo': f'{_ESPN}/atl.png'},
    'Miami Marlins':          {'color': '#00A3E0', 'accent': '#EF3340', 'logo': f'{_ESPN}/mia.png'},
    'New York Mets':          {'color': '#002D72', 'accent': '#FF5910', 'logo': f'{_ESPN}/nym.png'},
    'Philadelphia Phillies':  {'color': '#E81828', 'accent': '#002D72', 'logo': f'{_ESPN}/phi.png'},
    'Washington Nationals':   {'color': '#AB0003', 'accent': '#14225A', 'logo': f'{_ESPN}/wsh.png'},
    'Chicago Cubs':           {'color': '#0E3386', 'accent': '#CC3433', 'logo': f'{_ESPN}/chc.png'},
    'Cincinnati Reds':        {'color': '#C6011F', 'accent': '#000000', 'logo': f'{_ESPN}/cin.png'},
    'Milwaukee Brewers':      {'color': '#12284B', 'accent': '#FFC52F', 'logo': f'{_ESPN}/mil.png'},
    'Pittsburgh Pirates':     {'color': '#27251F', 'accent': '#FDB827', 'logo': f'{_ESPN}/pit.png'},
    'St. Louis Cardinals':    {'color': '#C41E3A', 'accent': '#0C2340', 'logo': f'{_ESPN}/stl.png'},
    'Arizona Diamondbacks':   {'color': '#A71930', 'accent': '#E3D4AD', 'logo': f'{_ESPN}/ari.png'},
    'Colorado Rockies':       {'color': '#33006F', 'accent': '#C4CED4', 'logo': f'{_ESPN}/col.png'},
    'Los Angeles Dodgers':    {'color': '#005A9C', 'accent': '#EF3E42', 'logo': f'{_ESPN}/lad.png'},
    'San Diego Padres':       {'color': '#2F241D', 'accent': '#FFC425', 'logo': f'{_ESPN}/sd.png'},
    'San Francisco Giants':   {'color': '#FD5A1E', 'accent': '#27251F', 'logo': f'{_ESPN}/sf.png'},
}

_WM = 'https://upload.wikimedia.org/wikipedia/en'
TEAM_INFO = {
    '統一7-ELEVEn獅': {'short': '統一獅', 'color': '#FF6B00', 'accent': '#003087', 'logo': f'{_WM}/8/83/Lions_Logo.png'},
    '富邦悍將':        {'short': '富邦',   'color': '#0050C8', 'accent': '#E8291C', 'logo': f'{_WM}/b/b6/Fubon_Guardians.png'},
    '味全龍':          {'short': '味全龍', 'color': '#C8102E', 'accent': '#111111', 'logo': f'{_WM}/9/93/Wei_Chuan_Dragons.png'},
    '台鋼雄鷹':        {'short': '台鋼',   'color': '#00843D', 'accent': '#1B2A4A', 'logo': f'{_WM}/f/f6/TSG_Hawks.png'},
    '中信兄弟':        {'short': '中信',   'color': '#F5C518', 'accent': '#1B2A4A', 'logo': f'{_WM}/d/d7/CTBC_Brothers_%28baseball_team%29_logo.png'},
    '樂天桃猿':        {'short': '樂天',   'color': '#E4002B', 'accent': '#FFFFFF', 'logo': f'{_WM}/8/8b/Rakuten_Monkeys.png'},
}

def safe_float(s, default=0.0):
    try:
        return float(s.strip())
    except Exception:
        return default

def safe_int(s, default=0):
    try:
        return int(s.strip())
    except Exception:
        return default


def parse_standings_table(table):
    rows = table.find_all('tr')
    if not rows:
        return []

    results = []
    for row in rows[1:]:
        tds = row.find_all('td')
        if len(tds) < 6:
            continue

        rank_div = tds[0].find('div', class_='rank')
        rank = rank_div.text.strip() if rank_div else ''

        team_link = tds[0].find('a')
        team_name = team_link.text.strip() if team_link else ''
        if not team_name:
            continue

        games   = tds[1].text.strip()
        wtl_raw = tds[2].text.strip()      # e.g. "3-0-1"
        win_rate_raw = tds[3].text.strip()
        gb_raw  = tds[4].text.strip()

        parts = wtl_raw.split('-')
        wins   = safe_int(parts[0]) if len(parts) > 0 else 0
        ties   = safe_int(parts[1]) if len(parts) > 1 else 0
        losses = safe_int(parts[2]) if len(parts) > 2 else 0
        win_rate = safe_float(win_rate_raw)

        # Last 4 columns: 主場, 客場, 連勝/連敗, 近十場
        home_rec   = tds[-4].text.strip() if len(tds) >= 4 else ''
        away_rec   = tds[-3].text.strip() if len(tds) >= 3 else ''
        streak     = tds[-2].text.strip() if len(tds) >= 2 else ''
        last10     = tds[-1].text.strip() if len(tds) >= 1 else ''

        info = TEAM_INFO.get(team_name, {'short': team_name, 'color': '#555', 'accent': '#aaa', 'logo': ''})

        results.append({
            'rank':      rank,
            'team':      team_name,
            'short':     info['short'],
            'color':     info['color'],
            'accent':    info['accent'],
            'logo':      info.get('logo', ''),
            'games':     safe_int(games),
            'wins':      wins,
            'ties':      ties,
            'losses':    losses,
            'win_rate':  win_rate,
            'gb':        gb_raw if gb_raw not in ('', '&nbsp;') else '-',
            'home':      home_rec,
            'away':      away_rec,
            'streak':    streak,
            'last10':    last10,
        })
    return results


def parse_stat_table(table, skip_first_col=True):
    rows = table.find_all('tr')
    if len(rows) < 2:
        return [], []

    headers = []
    for th in rows[0].find_all('th'):
        headers.append(th.text.strip())

    results = []
    for row in rows[1:]:
        tds = row.find_all('td')
        if not tds:
            continue
        entry = {}
        for i, td in enumerate(tds):
            if i < len(headers):
                # Team name cell
                link = td.find('a')
                val = link.text.strip() if link else td.text.strip()
                entry[headers[i]] = val
        if entry:
            results.append(entry)
    return headers, results


def scrape_all(year=None, season=None):
    with data_lock:
        year   = year   or data_store['year']
        season = season or data_store['season']

    try:
        url = 'https://www.cpbl.com.tw/standings/seasonaction'
        payload = {
            'KindCode':       'A',
            'SeasonCode':     season,
            'GameSystemCode': '',
            'YearCode':       year,
        }
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Referer':      'https://www.cpbl.com.tw/standings/season',
            'User-Agent':   'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        }

        resp = requests.post(url, data=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        resp.encoding = 'utf-8'

        soup = BeautifulSoup(resp.text, 'html.parser')
        tables = soup.find_all('table')

        standings = parse_standings_table(tables[0]) if len(tables) > 0 else []

        pitch_headers, pitching = [], []
        bat_headers, batting     = [], []
        field_headers, fielding  = [], []

        if len(tables) > 1:
            pitch_headers, pitching = parse_stat_table(tables[1])
        if len(tables) > 2:
            bat_headers, batting = parse_stat_table(tables[2])
        if len(tables) > 3:
            field_headers, fielding = parse_stat_table(tables[3])

        now = now_tw().strftime('%Y-%m-%d %H:%M:%S')
        with data_lock:
            data_store['standings']    = standings
            data_store['pitching']     = pitching
            data_store['pitch_cols']   = pitch_headers
            data_store['batting']      = batting
            data_store['bat_cols']     = bat_headers
            data_store['fielding']     = fielding
            data_store['field_cols']   = field_headers
            data_store['last_updated'] = now
            data_store['error']        = None
            data_store['year']         = year
            data_store['season']       = season

        print(f"[{now}] Updated — {len(standings)} teams, year={year}, season={season}")
        return True

    except Exception as e:
        err_msg = str(e)
        # Give a friendlier message for geo-blocked 404s
        if '404' in err_msg or 'Not Found' in err_msg:
            friendly = 'CPBL 伺服器暫時無法連線（可能為 IP 地區限制），顯示舊資料'
        else:
            friendly = f'CPBL 資料更新失敗：{err_msg}'
        with data_lock:
            data_store['error'] = friendly
        print(f"[ERROR scrape_all] {e}")
        return False


def scrape_games(year=None):
    """Fetch all games for the year and return today's + tomorrow's matches."""
    year = year or str(now_tw().year)
    try:
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        })

        # Get session cookies + AJAX token
        r = session.get('https://www.cpbl.com.tw/schedule', timeout=15)
        tokens = re.findall(r"RequestVerificationToken: '([^']+)'", r.text)
        if not tokens:
            raise ValueError('Cannot find RequestVerificationToken')
        ajax_token = tokens[0]

        r2 = session.post(
            'https://www.cpbl.com.tw/schedule/getgamedatas',
            data={'calendar': f'{year}/01/01', 'location': '', 'kindCode': 'A'},
            headers={
                'RequestVerificationToken': ajax_token,
                'X-Requested-With': 'XMLHttpRequest',
                'Referer': 'https://www.cpbl.com.tw/schedule',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            },
            timeout=15
        )
        r2.raise_for_status()
        result = r2.json()

        if not result.get('Success'):
            raise ValueError('API returned Success=false')

        all_games = json.loads(result['GameDatas'])
        today_str    = now_tw().strftime('%Y-%m-%d')
        tomorrow_str = (now_tw() + timedelta(days=1)).strftime('%Y-%m-%d')

        def build_game(g):
            gdate  = g.get('GameDate', '')[:10]
            status = g.get('GameResult', '')   # '0'=final, '2'=postponed
            return {
                'sno':          g.get('GameSno'),
                'date':         gdate,
                'time':         g.get('PreExeDate', '')[-8:-3] if g.get('PreExeDate') else '',
                'field':        g.get('FieldAbbe', ''),
                'visit_team':   g.get('VisitingTeamName', ''),
                'home_team':    g.get('HomeTeamName', ''),
                'visit_score':  g.get('VisitingScore'),
                'home_score':   g.get('HomeScore'),
                'is_final':     status == '0',
                'is_postponed': status == '2',
                'is_live':      g.get('IsPlayBall') == 'Y',
                'win_pitcher':    g.get('WinningPitcherName', ''),
                'lose_pitcher':   g.get('LoserPitcherName', ''),
                'closer':         g.get('CloserName', ''),
                'mvp':            g.get('MvpName', ''),
                'visit_starter':  g.get('VisitingPitcherName', ''),
                'home_starter':   g.get('HomePitcherName', ''),
                'visit_color':  TEAM_INFO.get(g.get('VisitingTeamName', ''), {}).get('color', '#555'),
                'home_color':   TEAM_INFO.get(g.get('HomeTeamName', ''),     {}).get('color', '#555'),
                'visit_logo':   TEAM_INFO.get(g.get('VisitingTeamName', ''), {}).get('logo', ''),
                'home_logo':    TEAM_INFO.get(g.get('HomeTeamName', ''),     {}).get('logo', ''),
            }

        yesterday_str  = (now_tw() - timedelta(days=1)).strftime('%Y-%m-%d')
        today_games    = [build_game(g) for g in all_games if g.get('GameDate', '')[:10] == today_str]
        tomorrow_games = [build_game(g) for g in all_games if g.get('GameDate', '')[:10] == tomorrow_str]
        yesterday_games= [build_game(g) for g in all_games if g.get('GameDate', '')[:10] == yesterday_str]

        now = now_tw().strftime('%Y-%m-%d %H:%M:%S')
        with data_lock:
            data_store['games']           = today_games
            data_store['tomorrow_games']  = tomorrow_games
            data_store['yesterday_games'] = yesterday_games
            data_store['games_updated']   = now

        print(f"[{now}] Games updated — yesterday:{len(yesterday_games)}, today:{len(today_games)}, tomorrow:{len(tomorrow_games)}")
        return True

    except Exception as e:
        print(f"[ERROR games] {e}")
        return False


def scrape_mlb_standings(year=None):
    year = year or str(now_et().year)
    try:
        url = f'https://statsapi.mlb.com/api/v1/standings?leagueId=103,104&season={year}&standingsType=regularSeason'
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()

        divisions = []
        for record in data.get('records', []):
            div_name = record.get('division', {}).get('name', '')
            teams = []
            for tr in record.get('teamRecords', []):
                team_obj = tr.get('team', {})
                name = team_obj.get('name', '')
                team_id = team_obj.get('id', 0)
                info = MLB_TEAM_COLORS.get(name, {'color': '#444', 'accent': '#888'})
                streak = tr.get('streak', {}).get('streakCode', '')
                teams.append({
                    'rank':     tr.get('divisionRank', ''),
                    'team':     name,
                    'color':    info['color'],
                    'accent':   info['accent'],
                    'logo':     _logo_url(team_id),
                    'wins':     tr.get('wins', 0),
                    'losses':   tr.get('losses', 0),
                    'games':    tr.get('gamesPlayed', 0),
                    'win_pct':  tr.get('winningPercentage', '.000'),
                    'gb':       tr.get('gamesBack', '-'),
                    'streak':   streak,
                    'wc_gb':    tr.get('wildCardGamesBack', '-'),
                })
            divisions.append({'name': div_name, 'teams': teams})

        now = now_tw().strftime('%Y-%m-%d %H:%M:%S')
        with mlb_lock:
            mlb_store['standings']    = divisions
            mlb_store['last_updated'] = now
            mlb_store['error']        = None
        print(f"[{now}] MLB standings updated — {len(divisions)} divisions")
        return True
    except Exception as e:
        with mlb_lock:
            mlb_store['error'] = str(e)
        print(f"[ERROR MLB standings] {e}")
        return False


def scrape_mlb_games(date=None):
    date = date or now_et().strftime('%Y-%m-%d')
    try:
        url = f'https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date}'
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()

        games = []
        for day in data.get('dates', []):
            for g in day.get('games', []):
                away = g['teams']['away']
                home = g['teams']['home']
                status = g.get('status', {})
                state  = status.get('abstractGameState', '')   # Final / Live / Preview
                detail = status.get('detailedState', '')

                is_final = (state == 'Final')
                is_live  = (state == 'Live')

                away_name = away.get('team', {}).get('name', '')
                home_name = home.get('team', {}).get('name', '')
                away_id = away.get('team', {}).get('id', 0)
                home_id = home.get('team', {}).get('id', 0)

                # game time → convert UTC to local +8 (Taiwan)
                game_utc = g.get('gameDate', '')
                game_time = ''
                if game_utc:
                    try:

                        dt = datetime.strptime(game_utc, '%Y-%m-%dT%H:%M:%SZ')
                        dt_tw = dt.replace(tzinfo=timezone.utc).astimezone(tz=timezone(timedelta(hours=8)))
                        game_time = dt_tw.strftime('%H:%M')
                    except Exception:
                        pass

                games.append({
                    'away_team':  away_name,
                    'home_team':  home_name,
                    'away_score': away.get('score', 0) if is_final or is_live else None,
                    'home_score': home.get('score', 0) if is_final or is_live else None,
                    'away_win':   away.get('isWinner', False),
                    'home_win':   home.get('isWinner', False),
                    'is_final':   is_final,
                    'is_live':    is_live,
                    'status':     detail,
                    'time':       game_time,
                    'venue':      g.get('venue', {}).get('name', ''),
                    'away_color': MLB_TEAM_COLORS.get(away_name, {}).get('color', '#444'),
                    'home_color': MLB_TEAM_COLORS.get(home_name, {}).get('color', '#444'),
                    'away_logo':  _logo_url(away_id),
                    'home_logo':  _logo_url(home_id),
                })

        now = now_tw().strftime('%Y-%m-%d %H:%M:%S')
        with mlb_lock:
            mlb_store['games']         = games
            mlb_store['games_updated'] = now
        print(f"[{now}] MLB games updated — {len(games)} games")
        return True
    except Exception as e:
        print(f"[ERROR MLB games] {e}")
        return False


prev_mlb_game_states = {}

def check_game_changes(new_games):
    """CPBL 開打 & 終場推播"""
    global prev_game_states
    for g in new_games:
        key = f"{g['date']}_{g['visit_team']}_{g['home_team']}"
        old = prev_game_states.get(key)

        # 開打通知
        if g['is_live'] and (old is None or not old.get('is_live')):
            msg = (
                f"⚾ CPBL 開打通知\n"
                f"{g['visit_team']} vs {g['home_team']}\n"
                f"📍 {g['field']}\n\n"
                f"📊 查看完整看板：\nhttps://cpbl-dashboard.fly.dev"
            )
            send_line(msg)

        # 終場通知
        if g['is_final'] and (old is None or not old.get('is_final')):
            winner = g['visit_team'] if g['visit_score'] > g['home_score'] else g['home_team']
            msg = (
                f"⚾ CPBL 終場通知\n"
                f"{g['visit_team']} {g['visit_score']} - {g['home_score']} {g['home_team']}\n"
                f"🏆 勝利：{winner}\n"
                f"勝投：{g['win_pitcher']} ／ 敗投：{g['lose_pitcher']}\n"
                f"MVP：{g['mvp']}\n\n"
                f"📊 查看完整看板：\nhttps://cpbl-dashboard.fly.dev"
            )
            send_line(msg)

        prev_game_states[key] = g


def check_mlb_game_changes(new_games):
    """MLB 比賽終場推播"""
    global prev_mlb_game_states
    today = now_et().strftime('%Y-%m-%d')
    for g in new_games:
        key = f"{today}_{g['away_team']}_{g['home_team']}"
        old = prev_mlb_game_states.get(key)
        if g['is_final'] and (old is None or not old.get('is_final')):
            away_score = g['away_score'] if g['away_score'] is not None else 0
            home_score = g['home_score'] if g['home_score'] is not None else 0
            winner = g['away_team'] if g['away_win'] else g['home_team']
            msg = (
                f"🇺🇸 MLB 終場通知\n"
                f"{g['away_team']} {away_score} - {home_score} {g['home_team']}\n"
                f"🏆 勝利：{winner}\n"
                f"📍 {g['venue']}\n\n"
                f"📊 查看完整看板：\nhttps://cpbl-dashboard.fly.dev"
            )
            send_line(msg)
        prev_mlb_game_states[key] = g


def background_updater():
    while True:
        time.sleep(10 * 60)   # every 10 min
        with data_lock:
            y = data_store['year']
            s = data_store['season']
        scrape_all(y, s)
        scrape_games(y)
        with data_lock:
            check_game_changes(data_store['games'])
        scrape_mlb_standings()
        scrape_mlb_games()
        with mlb_lock:
            check_mlb_game_changes(mlb_store['games'])


# ── 啟動初始化（Gunicorn 也會執行）────────────────────────────────────────────
print("[STARTUP] Initializing data...")
scrape_all()
scrape_games()
scrape_mlb_standings()
scrape_mlb_games()

_bg_thread = threading.Thread(target=background_updater, daemon=True)
_bg_thread.start()
print("[STARTUP] Background updater started.")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/webhook', methods=['POST'])
def webhook():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        line_handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

def get_cpbl_schedule_text():
    with data_lock:
        games = data_store.get('games', [])
    if not games:
        return '今日無 CPBL 賽事'
    today = now_tw().strftime('%Y/%m/%d')
    weekdays = ['一','二','三','四','五','六','日']
    wd = weekdays[now_tw().weekday()]
    lines = [f'⚾ CPBL 今日賽程　{today}（週{wd}）\n']
    for g in games:
        if g['is_final']:
            status = f"終場　{g['visit_score']} - {g['home_score']}"
        elif g['is_live']:
            status = '進行中'
        else:
            status = f"🕐 {g['time']} 開打"
        lines.append(f"{g['visit_team']} vs {g['home_team']}")
        lines.append(f"{status}　📍{g['field']}\n")
    lines.append(f"📊 {data_store.get('last_updated','')[:10]}")
    lines.append('https://cpbl-dashboard.fly.dev')
    return '\n'.join(lines)


def get_mlb_schedule_text():
    with mlb_lock:
        games = mlb_store.get('games', [])
    if not games:
        return '今日無 MLB 賽事'
    today = now_et().strftime('%Y/%m/%d')
    weekdays = ['一','二','三','四','五','六','日']
    wd = weekdays[now_et().weekday()]
    lines = [f'🇺🇸 MLB 今日賽程　{today}（週{wd}）\n']
    for g in games:
        if g['is_final']:
            status = f"終場　{g['away_score']} - {g['home_score']}"
        elif g['is_live']:
            status = g.get('status', '進行中')
        else:
            status = f"🕐 {g['time']}"
        lines.append(f"{g['away_team']} @ {g['home_team']}")
        lines.append(f"{status}\n")
    lines.append('https://cpbl-dashboard.fly.dev')
    return '\n'.join(lines)


@line_handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    uid = event.source.user_id
    line_user_ids.add(uid)
    save_user_ids()   # 立即寫檔，重啟後不會遺失
    text = event.message.text.strip()

    cpbl_keywords = ['cpbl', '中華職棒', '台灣', '職棒']
    mlb_keywords  = ['mlb', '美國職棒', '大聯盟']

    if any(k in text.lower() for k in cpbl_keywords):
        reply = get_cpbl_schedule_text()
    elif any(k in text.lower() for k in mlb_keywords):
        reply = get_mlb_schedule_text()
    else:
        # 第一次加入時的歡迎訊息
        if not hasattr(handle_message, '_welcomed'):
            handle_message._welcomed = set()
        if uid not in handle_message._welcomed:
            handle_message._welcomed.add(uid)
            reply = (
                "✅ 職棒看板已綁定！比賽結束時會自動通知你。\n\n"
                "你可以傳送：\n"
                "・「CPBL賽程」→ 今日中華職棒\n"
                "・「MLB賽程」→ 今日美國職棒\n\n"
                "📊 https://cpbl-dashboard.fly.dev"
            )
        else:
            reply = (
                "你可以傳送：\n"
                "・「CPBL賽程」→ 今日中華職棒\n"
                "・「MLB賽程」→ 今日美國職棒"
            )

    print(f"[LINE] User: {uid}, msg: {text}")
    # 使用 reply_message（免費無限次），不消耗 push 額度
    with ApiClient(line_config) as api_client:
        api = MessagingApi(api_client)
        api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text=reply)]
        ))

@app.route('/debug')
def debug():
    import os
    path = os.path.join(app.root_path, 'templates', 'index.html')
    with open(path, encoding='utf-8') as f:
        content = f.read()
    return f"root={app.root_path} | size={len(content)} | has_mlb={'league-switcher' in content}"

@app.route('/')
def index():
    resp = render_template('index.html')
    from flask import make_response
    r = make_response(resp)
    r.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return r


@app.route('/api/data')
def api_data():
    with data_lock:
        return jsonify({k: v for k, v in data_store.items()})


@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    body    = request.get_json(silent=True) or {}
    year    = body.get('year',   str(now_tw().year))
    season  = body.get('season', '1')
    scrape_all(year, season)
    scrape_games(year)
    with data_lock:
        return jsonify({k: v for k, v in data_store.items()})


@app.route('/api/mlb')
def api_mlb():
    with mlb_lock:
        return jsonify({k: v for k, v in mlb_store.items()})


@app.route('/api/mlb/refresh', methods=['POST'])
def api_mlb_refresh():
    scrape_mlb_standings()
    scrape_mlb_games()
    with mlb_lock:
        return jsonify({k: v for k, v in mlb_store.items()})


if __name__ == '__main__':
    print("CPBL Dashboard → http://127.0.0.1:8080")
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
