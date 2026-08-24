import urllib.request
import urllib.parse
import json

def fetch_job_avail(job_name, minutes=1440):
    base = 'http://192.168.40.228:5000/api/availability'
    params = urllib.parse.urlencode({'minutes': minutes, 'job': job_name})
    url = f"{base}?{params}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        return {'error': str(e)}

def main():
    jobs = ['blackbox-ping-internal', 'blackbox-ping-external', 'blackbox', 'blackbox_http', 'custom']
    print(f"{'Job Name':<25} | {'Fleet Average':<14} | {'Fleet Aggregate':<16} | {'Target (Up/Down/Total)'}")
    print("-" * 75)
    for j in jobs:
        data = fetch_job_avail(j, 1440)
        if 'error' in data:
            print(f"{j:<25} | Error: {data['error']}")
            continue
        avg = f"{data.get('fleet_average', {}).get('value')}%"
        agg = f"{data.get('fleet_aggregate', {}).get('value')}%"
        c = data.get('counts', {})
        t_str = f"{c.get('online')} Up / {c.get('offline')} Down / {c.get('total')} Total"
        print(f"{j:<25} | {avg:<14} | {agg:<16} | {t_str}")

if __name__ == '__main__':
    main()
