import argparse
import os
import re
from zipfile import ZipFile
import requests
from github import Auth, Github, Repository
from get_most_recent_ci_hash import gcc_hashes, get_valid_artifact_hash
from compare_testsuite_log import compare_logs


def parse_arguments():
    """ parse command line arguments """
    parser = argparse.ArgumentParser(description="Download valid log artifacts")
    parser.add_argument(
        "-hash",
        required=True,
        type=str,
        help="Commit hash of GCC to get artifacts for",
    )
    parser.add_argument(
        "-phash",
        required=False,
        type=str,
        help="Previous commit hash if exists",
    )
    parser.add_argument(
        "-token",
        required=True,
        type=str,
        help="Github access token",
    )
    return parser.parse_args()


def get_possible_artifact_names():
    """
    Generates all possible permutations of target artifact logs and
    removes unsupported targets

    Current Support:
      Linux: rv32/64 multilib non-multilib
      Newlib: rv32/64 non-multilib
      Arch extensions: gc
    """
    libc = ["gcc-linux", "gcc-newlib"]
    arch = ["rv32{}-ilp32d-{}", "rv64{}-lp64d-{}"]
    multilib = ["multilib", "non-multilib"]

    arch_extensions = [
        "gc",
        "gcv",
        "gc_zba_zbb_zbc_zbs",
        "gcv_zvbb_zvbc_zvkg_zvkn_zvknc_zvkned_zvkng_zvknha_zvknhb_zvks_zvksc_zvksed_zvksg_zvksh_zvkt",
        "imafdcv_zicond_zawrs_zbc_zvkng_zvksg_zvbb_zvbc_zicsr_zba_zbb_zbs_zicbom_zicbop_zicboz_zfhmin_zkt"
    ]

    all_lists = [
        "-".join([i, j, k])
        for i in libc
        for j in arch
        for k in multilib
        if not ("rv32" in j and k == "multilib")
    ]

    all_names = [
        name.format(ext, "{}") for name in all_lists for ext in arch_extensions
        if not ("gcv" in ext and "non-multilib" not in name)
        and not ("gc_" in ext and "non-multilib" not in name)
        and not ("imafdcv_" in ext and "non-multilib" not in name)
        and not ("rv32" in name and "imafdcv_zicond_zawrs_zbc_zvkng_zvksg_zvbb_zvbc_zicsr_zba_zbb_zbs_zicbom_zicbop_zicboz_zfhmin_zkt" in ext)
        and not ("non-multilib" in name and ext == "gc")
    ]
    return all_names


def check_artifact_exists(artifact_name: str):
    """
    @param artifact_name is the artifact associated with build success
    If the artifact does not exist, something failed and logs error into
    appropriate file
    """
    build_name = f"{artifact_name}.zip"
    log_name = f"{artifact_name}-report.log"
    build_failed = False
    # check if the build failed
    if (not os.path.exists(os.path.join("./temp", build_name)) and
        not os.path.exists(os.path.join("./current_logs", log_name))):
        print(f"build failed for {artifact_name}")
        build_failed = True
        with open("./current_logs/failed_build.txt", "a+") as f:
            f.write(f"{artifact_name}|Check logs\n")

    # check if the testsuite failed
    if not os.path.exists(os.path.join("./current_logs", log_name)):
        print(f"testsuite failed for {artifact_name}")
        if not build_failed:
            with open("./current_logs/failed_testsuite.txt", "a+") as f:
                f.write(f"{artifact_name}|Cannot find testsuite artifact. Likely caused by testsuite timeout.\n")
        return False
    return True


def download_artifact(artifact_name: str, artifact_id: str, token: str, outdir: str = "current_logs"):
    """
    Uses GitHub api endpoint to download and extract the previous workflow
    log artifacts into directory called ./logs. Current workflow log artifacts
    are already stored in ./logs
    """
    params = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"token {token}",
        "X-Github-Api-Version": "2022-11-28",
    }
    artifact_zip_name = artifact_name.replace(".log", ".zip")
    r = requests.get(
        f"https://api.github.com/repos/rivosinc/riscv-gnu-toolchain-gcc-ci/actions/artifacts/{artifact_id}/zip",
        headers=params,
    )
    print(f"download for {artifact_zip_name}: {r.status_code}")
    download_binary = False
    with open(f"./temp/{artifact_zip_name}", "wb") as f:
        f.write(r.content)
    with ZipFile(f"./temp/{artifact_zip_name}", "r") as zf:
        try:
            zf.extractall(path=f"./temp/{artifact_name.split('.log')[0]}")
        except NotADirectoryError:
            download_binary = True
            print("extracting a binary file")
            zf.extractall(path="./temp/")
    if not download_binary:
        os.rename(
            f"./temp/{artifact_name.split('.log')[0]}/{artifact_name}", f"./{outdir}/{artifact_name}"
        )


def download_all_artifacts(current_hash: str, previous_hash: str, token: str):
    """
    Goes through all possible artifact targets and downloads it
    if it exists. Downloads previous successful hash's artifact
    as well. Runs comparison on the downloaded artifacts
    """

    prev_commits = gcc_hashes(current_hash, False)
    artifact_names = get_possible_artifact_names()
    for artifact_name in artifact_names:
        artifact = artifact_name.format(current_hash)
        if not check_artifact_exists(artifact):
            continue

        # comparison output path
        compare_path = f"./summaries/{artifact + '-report-summary.md'}"
        artifact += "-report.log"
        artifact_name += "-report.log"

        # check if we already have a previous artifact available
        # mostly for regenerate issues
        if previous_hash:
            name_components = artifact_name.split("-")
            name_components[4] = "{}"
            previous_name = "-".join(name_components).format(previous_hash)
            name_components[4] = "[^-]*"
            name_regex = "-".join(name_components)
            dir_contents = " ".join(os.listdir("./previous_logs"))
            possible_previous_logs = re.findall(name_regex, dir_contents)
            if len(possible_previous_logs) > 1:
                print(f"found more than 1 previous log for {name_regex}: {possible_previous_logs}")
                for log in possible_previous_logs: # remove non-recent logs
                    if previous_name not in log:
                        print(f"removing {log} from previous_logs")
                        os.remove(os.path.join("./previous_logs", log))
                continue
            if len(possible_previous_logs) == 1:
                print(f"found single log: {possible_previous_logs[0]}. Skipping download")
                continue

        # download previous artifact
        base_hash, base_id = get_valid_artifact_hash(prev_commits, token, artifact_name)
        if base_hash != "No valid hash":
            download_artifact(artifact_name.format(base_hash), str(base_id), token, "previous_logs")
        else:
            print(f"found no valid hash for {artifact_name}. possible hashes: {prev_commits}")

def main():
    args = parse_arguments()
    download_all_artifacts(args.hash, args.phash, args.token)


if __name__ == "__main__":
    main()
