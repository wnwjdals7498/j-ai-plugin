"""Compare whole-suite JUnit test IDs without exporting failure values."""
import argparse,json
import xml.etree.ElementTree as ET
from pathlib import Path


def report(path):
    root=ET.parse(path).getroot()
    cases={}
    for case in root.iter("testcase"):
        key=case.get("classname","")+"::"+case.get("name","")
        state="passed"
        for tag in ("error","failure","skipped"):
            if case.find(tag) is not None:
                state=tag
                break
        cases[key]=state
    counts={state:sum(value==state for value in cases.values()) for state in ("passed","failure","error","skipped")}
    return cases,counts


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--baseline",required=True)
    parser.add_argument("--current",required=True)
    parser.add_argument("--out",required=True)
    args=parser.parse_args()
    before,before_counts=report(args.baseline)
    after,after_counts=report(args.current)
    bad={"failure","error"}
    new_failures=sorted(key for key,state in after.items() if state in bad and before.get(key) not in bad)
    preserved=sorted(key for key,state in after.items() if state in bad and before.get(key) in bad)
    resolved=sorted(key for key,state in before.items() if state in bad and after.get(key)=="passed")
    missing=sorted(set(before)-set(after))
    value={"baseline":before_counts,"current":after_counts,"new_failures":new_failures,
           "preserved_problems":preserved,"resolved_problems":resolved,"missing_baseline_tests":missing,
           "new_tests":len(set(after)-set(before)),
           "comparison_pass":not new_failures and not missing}
    Path(args.out).write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(value,ensure_ascii=False,indent=2))
    raise SystemExit(0 if value["comparison_pass"] else 1)
