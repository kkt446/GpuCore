
from __future__ import annotations

import os
import shutil
import sys
import zipfile

DEFAULT_DROP = ("使用说明.txt", "README-portable.txt")


def strip(zip_path: str, drop=DEFAULT_DROP) -> int:
    if not os.path.isfile(zip_path):
        print(f"找不到文件：{zip_path}")
        return 1

    drop_set = set(drop)
    tmp_path = zip_path + ".tmp"
    removed: list[str] = []
    kept = 0

    with zipfile.ZipFile(zip_path, "r") as src:
        infos = src.infolist()
        to_drop = {
            i.filename for i in infos
            if i.filename in drop_set and "/" not in i.filename and "\\" not in i.filename
        }
        if not to_drop:
            print(f"无需改动（包里没有 {', '.join(sorted(drop_set))}）：{zip_path}")
            return 0

        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as dst:
            dst.comment = src.comment
            for info in infos:
                if info.filename in to_drop:
                    removed.append(info.filename)
                    continue
                data = src.read(info.filename)
                new_info = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                new_info.compress_type = zipfile.ZIP_DEFLATED
                new_info.external_attr = info.external_attr
                new_info.internal_attr = info.internal_attr
                new_info.create_system = info.create_system
                dst.writestr(new_info, data)
                kept += 1

    try:
        os.replace(tmp_path, zip_path)
    except OSError:
        shutil.move(tmp_path, zip_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    print(f"已更新：{zip_path}")
    print(f"  剔除 {len(removed)} 项：{', '.join(removed)}")
    print(f"  保留 {kept} 项")
    return 0


def main(argv: list[str]) -> int:
    args = argv[1:]
    drop = DEFAULT_DROP
    if "--drop" in args:
        i = args.index("--drop")
        drop = tuple(args[i + 1:])
        args = args[:i]
    if not args:
        print(__doc__)
        return 2
    rc = 0
    for p in args:
        rc |= strip(p, drop)
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
