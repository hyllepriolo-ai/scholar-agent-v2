import os, sys, csv
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backend.services.doi_resolver import resolve_doi
from backend.services.author_extractor import extract_authors

csv_path = r"c:\Users\M\Desktop\skills\csv-CellJourna-set.csv"
DOIS = []
with open(csv_path, 'r', encoding='utf-8') as f:
    reader = csv.DictReader(f)
    for i, row in enumerate(reader):
        if i >= 50: break
        if row.get('DOI'): DOIS.append(row['DOI'])

# Parse test_50_log.txt results
results_map = {}
if os.path.exists('test_50_log.txt'):
    with open('test_50_log.txt', 'r', encoding='utf-8') as f:
        lines = f.readlines()
        for line in lines:
            line = line.strip()
            if '|' in line and not line.startswith('----'):
                # 10.1016/j.cell.2025.11.002 | 通讯:HIT | 一作:HIT
                parts = line.split('|')
                if len(parts) == 3:
                    doi = parts[0].strip()
                    corr_hit = parts[1].split(':')[1].strip()
                    first_hit = parts[2].split(':')[1].strip()
                    results_map[doi] = {'corr_hit': corr_hit, 'first_hit': first_hit}

tested_dois = list(results_map.keys())
print(f"Total tested DOIs in log: {len(tested_dois)}")

diff_authors = 0
diff_hits = 0
diff_miss = 0

print("-" * 60)
print(f"{'DOI':<30} | {'第一作者':<20} | {'通讯作者':<20} | 一作结果")
print("-" * 60)

for doi in tested_dois:
    try:
        meta = resolve_doi(doi)
        author_data = extract_authors(meta)
        fn = author_data.get('第一作者', {}).get('姓名', '?')
        cn = author_data.get('通讯作者', {}).get('姓名', '?')
        
        # 判断是否是非一作通讯重名的一作（即一作 != 通讯作者）
        if fn != cn and fn != '?' and fn != '未找到':
            diff_authors += 1
            fhit = results_map[doi]['first_hit']
            if fhit == 'HIT':
                diff_hits += 1
            else:
                diff_miss += 1
            print(f"{doi:<30} | {fn[:18]:<20} | {cn[:18]:<20} | {fhit}")
    except Exception as e:
        print(f"{doi} 处理出错: {e}")

print("-" * 60)
print(f"统计汇总:")
print(f"总计不同名的 一作 数量 (非一作=通讯): {diff_authors}")
print(f" -> 命中 (HIT): {diff_hits}")
print(f" -> 失败 (MISS): {diff_miss}")
print(f" -> 命中率: {diff_hits/diff_authors*100:.1f}%" if diff_authors > 0 else "")

