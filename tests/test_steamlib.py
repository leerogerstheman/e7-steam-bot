"""Steam 库定位测试。

用**构造出来的假 Steam 目录树**验证解析逻辑（不依赖开发机上真的装了 Steam），
另外用一组"实测已知值"锁住 AppID 与主程序名。
"""

from __future__ import annotations

from pathlib import Path

from e7bot import steamlib as S

#: 实测值
REAL_APPID_DEMO = "5129800"
REAL_APPID_FULL = "5019180"
REAL_EXE = "EpicSeven_Steam.exe"


def _make_library(root: Path, appid: str, installdir: str, name: str = "Epic Seven",
                  size: int = 135467955, state: int = 4) -> Path:
    """造一个最小可用的 Steam 库：steamapps/libraryfolders.vdf + appmanifest。"""
    (root / "steamapps" / "common" / installdir).mkdir(parents=True, exist_ok=True)
    (root / "steamapps" / f"appmanifest_{appid}.acf").write_text(
        '{\n'
        f'\t"appid"\t\t"{appid}"\n'
        f'\t"name"\t\t"{name}"\n'
        f'\t"StateFlags"\t\t"{state}"\n'
        f'\t"installdir"\t\t"{installdir}"\n'
        f'\t"SizeOnDisk"\t\t"{size}"\n'
        '}\n',
        encoding="utf-8",
    )
    return root


def _write_libraryfolders(steam_root: Path, libs: list[Path]) -> None:
    (steam_root / "steamapps").mkdir(parents=True, exist_ok=True)
    body = "".join(
        f'\t"{i}"\n\t{{\n\t\t"path"\t\t"{str(p).replace(chr(92), chr(92)*2)}"\n\t}}\n'
        for i, p in enumerate(libs)
    )
    (steam_root / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n' + body + '}\n', encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #


def test_appids_match_reality() -> None:
    assert S.APPID_DEMO == REAL_APPID_DEMO
    assert S.APPID_FULL == REAL_APPID_FULL


def test_main_exe_name_matches_reality() -> None:
    """主程序名是从 Demo 二进制/目录里实测出来的，不能随手改。"""
    assert S.MAIN_EXE == REAL_EXE


def test_anticheat_filenames_match_reality() -> None:
    assert S.ANTICHEAT_LOADER == "ucldr_Epic7_SM_loader_x64.exe"
    assert S.ANTICHEAT_MODULE == "xnina_x64.xem"


# --------------------------------------------------------------------------- #
# libraryfolders.vdf 解析
# --------------------------------------------------------------------------- #


def test_list_library_folders_parses_vdf(tmp_path: Path) -> None:
    steam = tmp_path / "Steam"
    lib2 = tmp_path / "SteamLibrary"
    _make_library(steam, REAL_APPID_DEMO, "Epic Seven Demo")
    _make_library(lib2, REAL_APPID_FULL, "Epic Seven")
    _write_libraryfolders(steam, [steam, lib2])

    libs = S.list_library_folders(steam)
    assert steam in libs
    assert lib2 in libs


def test_list_library_folders_skips_nonexistent(tmp_path: Path) -> None:
    steam = tmp_path / "Steam"
    _make_library(steam, REAL_APPID_DEMO, "Epic Seven Demo")
    _write_libraryfolders(steam, [steam, tmp_path / "NotThere"])

    libs = S.list_library_folders(steam)
    assert steam in libs
    assert not any("NotThere" in str(p) for p in libs)


def test_list_library_folders_without_vdf_returns_root(tmp_path: Path) -> None:
    steam = tmp_path / "Steam"
    (steam / "steamapps").mkdir(parents=True)
    assert S.list_library_folders(steam) == [steam]


def test_list_library_folders_handles_garbage_vdf(tmp_path: Path) -> None:
    steam = tmp_path / "Steam"
    (steam / "steamapps").mkdir(parents=True)
    (steam / "steamapps" / "libraryfolders.vdf").write_text(
        "this is not a vdf {{{", encoding="utf-8"
    )
    # 不该抛异常，至少要退回默认库
    assert S.list_library_folders(steam) == [steam]


def test_find_steam_root_returns_path_or_none() -> None:
    root = S.find_steam_root()
    assert root is None or root.is_dir()


# --------------------------------------------------------------------------- #
# 找安装
# --------------------------------------------------------------------------- #


def test_find_install_demo(tmp_path: Path) -> None:
    steam = tmp_path / "Steam"
    _make_library(steam, REAL_APPID_DEMO, "Epic Seven Demo", name="Epic Seven Demo")
    _write_libraryfolders(steam, [steam])

    inst = S.find_install(REAL_APPID_DEMO, steam)
    assert inst is not None
    assert inst.appid == REAL_APPID_DEMO
    assert inst.is_demo is True
    assert inst.installdir == steam / "steamapps" / "common" / "Epic Seven Demo"
    assert inst.size_on_disk == 135467955
    assert inst.fully_installed is True


def test_find_install_across_multiple_libraries(tmp_path: Path) -> None:
    steam = tmp_path / "Steam"
    lib_f = tmp_path / "F" / "SteamLibrary"
    _make_library(steam, "999", "Other Game", name="Other")
    _make_library(lib_f, REAL_APPID_DEMO, "Epic Seven Demo")
    _write_libraryfolders(steam, [steam, lib_f])

    inst = S.find_install(REAL_APPID_DEMO, steam)
    assert inst is not None
    assert inst.library == lib_f


def test_find_install_missing_returns_none(tmp_path: Path) -> None:
    steam = tmp_path / "Steam"
    _make_library(steam, "999", "Other Game")
    _write_libraryfolders(steam, [steam])
    assert S.find_install(REAL_APPID_DEMO, steam) is None


def test_find_epic_seven_prefers_full_over_demo(tmp_path: Path) -> None:
    steam = tmp_path / "Steam"
    _make_library(steam, REAL_APPID_DEMO, "Epic Seven Demo")
    _make_library(steam, REAL_APPID_FULL, "Epic Seven")
    _write_libraryfolders(steam, [steam])

    inst = S.find_epic_seven(steam)
    assert inst is not None
    assert inst.appid == REAL_APPID_FULL, "正式版应该优先于 Demo"


def test_find_epic_seven_falls_back_to_demo(tmp_path: Path) -> None:
    steam = tmp_path / "Steam"
    _make_library(steam, REAL_APPID_DEMO, "Epic Seven Demo")
    _write_libraryfolders(steam, [steam])
    inst = S.find_epic_seven(steam)
    assert inst is not None and inst.is_demo


def test_find_all_returns_both(tmp_path: Path) -> None:
    steam = tmp_path / "Steam"
    _make_library(steam, REAL_APPID_DEMO, "Epic Seven Demo")
    _make_library(steam, REAL_APPID_FULL, "Epic Seven")
    _write_libraryfolders(steam, [steam])
    assert len(S.find_all(steam)) == 2


# --------------------------------------------------------------------------- #
# 安装详情
# --------------------------------------------------------------------------- #


def test_main_exe_found_when_present(tmp_path: Path) -> None:
    steam = tmp_path / "Steam"
    lib = _make_library(steam, REAL_APPID_DEMO, "Epic Seven Demo")
    (lib / "steamapps" / "common" / "Epic Seven Demo" / REAL_EXE).write_bytes(b"MZ")
    _write_libraryfolders(steam, [steam])

    inst = S.find_install(REAL_APPID_DEMO, steam)
    assert inst is not None
    assert inst.main_exe is not None
    assert inst.main_exe.name == REAL_EXE


def test_main_exe_none_when_missing(tmp_path: Path) -> None:
    steam = tmp_path / "Steam"
    _make_library(steam, REAL_APPID_DEMO, "Epic Seven Demo")
    _write_libraryfolders(steam, [steam])
    inst = S.find_install(REAL_APPID_DEMO, steam)
    assert inst is not None and inst.main_exe is None


def test_anticheat_detection(tmp_path: Path) -> None:
    steam = tmp_path / "Steam"
    lib = _make_library(steam, REAL_APPID_DEMO, "Epic Seven Demo")
    d = lib / "steamapps" / "common" / "Epic Seven Demo"
    _write_libraryfolders(steam, [steam])

    inst = S.find_install(REAL_APPID_DEMO, steam)
    assert inst is not None
    assert inst.has_anticheat is False

    (d / S.ANTICHEAT_LOADER).write_bytes(b"MZ")
    assert inst.has_anticheat is True


def test_state_flags_partial_install(tmp_path: Path) -> None:
    steam = tmp_path / "Steam"
    _make_library(steam, REAL_APPID_DEMO, "Epic Seven Demo", state=1026)
    _write_libraryfolders(steam, [steam])
    inst = S.find_install(REAL_APPID_DEMO, steam)
    assert inst is not None and inst.fully_installed is False


def test_describe_contains_key_fields(tmp_path: Path) -> None:
    steam = tmp_path / "Steam"
    lib = _make_library(steam, REAL_APPID_DEMO, "Epic Seven Demo", name="Epic Seven Demo")
    (lib / "steamapps" / "common" / "Epic Seven Demo" / REAL_EXE).write_bytes(b"MZ")
    _write_libraryfolders(steam, [steam])

    inst = S.find_install(REAL_APPID_DEMO, steam)
    assert inst is not None
    text = inst.describe()
    assert REAL_APPID_DEMO in text
    assert "Demo" in text
    assert REAL_EXE in text
    assert "129 MB" in text          # 135467955 字节
