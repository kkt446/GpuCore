r"""从便携包 zip 里剔除不需要的文件（说明书等），其余条目原样保留。

为什么要单独写个脚本而不是解压再压：zip 里有 6980 个条目，解压重压既慢又占空间，
而 zip 格式本身就支持逐条复制。这里用 zipfile 流式重写，压缩级别与原包保持一致。

用法：
    python tools/strip-bundle-docs.py <zip 路径> [更多 zip 路径]
    python tools/strip-bundle-docs.py <zip> --drop 使用说明.txt README-portable.txt

默认剔除：
    使用说明.txt / README-portable.txt
    —— 这两份说明现在直接放在仓库里，包里不再重复带一份（避免同一份文档两处维护）。

边界处理：
    * 只删顶层同名文件，不动 python\ 或 gpucore\ 里可能存在的同名文件。
    * 没有可删的就直接退出，不改动文件（幂等）。
    * 先在同目录写 .tmp 再 os.replace 换上去，中途失败不会留下半个坏包。
    * 原包的注释（如果有）会被保留。
"""

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
        # 只认顶层同名文件（条目名里不该再有分隔符）
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
                # 保留原始时间戳与属性位，避免解压后时间全变成"现在"
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
        # 直接覆盖被拒时，退一步用 shutil.move 再试
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
