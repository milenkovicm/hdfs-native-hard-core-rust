import functools
import os
import re
import shutil
import sys
from argparse import ArgumentParser, Namespace
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from hdfs_native import Client


@functools.cache
def _get_client(connection_url: Optional[str] = None):
    return Client(connection_url)


def _client_for_url(url: str) -> Client:
    parsed = urlparse(url)

    if parsed.scheme:
        connection_url = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port:
            connection_url += f":{parsed.port}"
        return _get_client(connection_url)
    elif parsed.hostname or parsed.port:
        raise ValueError(
            f"Cannot provide host or port without scheme: {parsed.hostname}"
        )
    else:
        return _get_client()


def _verify_nameservices_match(url: str, *urls: str) -> None:
    first = urlparse(url)

    for url in urls:
        parsed = urlparse(url)
        if first.scheme != parsed.scheme or first.hostname != parsed.hostname:
            raise ValueError(
                f"Protocol and host must match: {first.scheme}://{first.hostname} != {parsed.scheme}://{parsed.hostname}"
            )


def _path_for_url(url: str) -> str:
    return urlparse(url).path


def _glob_path(client: Client, glob: str) -> List[str]:
    # TODO: Actually implement this, for now just pretend we have multiple results
    return [glob]


def _download_file(
    client: Client,
    remote_src: str,
    local_dst: str,
    force: bool = False,
    preserve: bool = False,
) -> None:
    if not force and os.path.exists(local_dst):
        raise FileExistsError(f"{local_dst} already exists, use --force to overwrite")

    with client.read(remote_src) as remote_file:
        with open(local_dst, "wb") as local_file:
            for chunk in remote_file.read_range_stream(0, len(remote_file)):
                local_file.write(chunk)

    if preserve:
        status = client.get_file_info(remote_src)
        os.utime(
            local_dst,
            (status.access_time / 1000, status.modification_time / 1000),
        )
        os.chmod(local_dst, status.permission)


def _upload_file(
    client: Client,
    local_src: str,
    remote_dst: str,
    force: bool = False,
    preserve: bool = False,
) -> None:
    with open(local_src, "rb") as local_file:
        with client.create(remote_dst) as remote_file:
            shutil.copyfileobj(local_file, remote_file)


def cat(args: Namespace):
    for src in args.src:
        client = _client_for_url(src)
        for path in _glob_path(client, _path_for_url(src)):
            with client.read(path) as file:
                while chunk := file.read(1024 * 1024):
                    sys.stdout.buffer.write(chunk)

    sys.stdout.buffer.flush()


def chmod(args: Namespace):
    if not re.fullmatch(r"1?[0-7]{3}", args.octalmode):
        raise ValueError(f"Invalid mode supplied: {args.octalmode}")

    permission = int(args.octalmode, base=8)

    for url in args.path:
        client = _client_for_url(url)
        for path in _glob_path(client, _path_for_url(url)):
            if args.recursive:
                for status in client.list_status(path, True):
                    client.set_permission(status.path, permission)
            else:
                client.set_permission(path, permission)


def chown(args: Namespace):
    split = args.owner.split(":")
    if len(split) > 2:
        raise ValueError(f"Invalid owner and group pattern: {args.owner}")

    if len(split) == 1:
        owner = split[0]
        group = None
    else:
        owner = split[0] if split[0] else None
        group = split[1]

    for url in args.path:
        client = _client_for_url(url)
        for path in _glob_path(client, _path_for_url(url)):
            if args.recursive:
                for status in client.list_status(path, True):
                    client.set_owner(status.path, owner, group)
            else:
                client.set_owner(path, owner, group)


def get(args: Namespace):
    paths: List[Tuple[Client, str]] = []

    for url in args.src:
        client = _client_for_url(url)
        for path in _glob_path(client, _path_for_url(url)):
            paths.append((client, path))

    dst_is_dir = os.path.isdir(args.localdst)

    if len(paths) > 1 and not dst_is_dir:
        raise ValueError("Destination must be directory when copying multiple files")
    elif not dst_is_dir:
        _download_file(
            paths[0][0],
            paths[0][1],
            args.localdst,
            force=args.force,
            preserve=args.preserve,
        )
    else:
        with ThreadPoolExecutor(args.threads) as executor:
            futures = []
            for client, path in paths:
                filename = os.path.basename(path)

                futures.append(
                    executor.submit(
                        _download_file,
                        client,
                        path,
                        os.path.join(args.localdst, filename),
                        force=args.force,
                        preserve=args.preserve,
                    )
                )

            # Iterate to raise any exceptions thrown
            for f in as_completed(futures):
                f.result()


def mkdir(args: Namespace):
    create_parent = args.parent

    for url in args.path:
        client = _client_for_url(url)
        client.mkdirs(_path_for_url(url), create_parent=create_parent)


def mv(args: Namespace):
    _verify_nameservices_match(args.dst, *args.src)

    client = _client_for_url(args.dst)
    dst_path = _path_for_url(args.dst)

    dst_isdir = False
    try:
        dst_isdir = client.get_file_info(dst_path).isdir
    except FileNotFoundError:
        pass

    resolved_src = [
        path
        for pattern in args.src
        for path in _glob_path(client, _path_for_url(pattern))
    ]

    if len(resolved_src) > 1 and not dst_isdir:
        raise ValueError(
            "destination must be a directory if multiple sources are provided"
        )

    for src in resolved_src:
        src_path = _path_for_url(src)
        if dst_isdir:
            target_path = os.path.join(dst_path, os.path.basename(src_path))
        else:
            target_path = dst_path

        client.rename(src_path, target_path)


def main(in_args: Optional[Sequence[str]] = None):
    parser = ArgumentParser(
        description="""Command line utility for interacting with HDFS using hdfs-native.
        Globs are not currently supported, all file paths are treated as exact paths."""
    )

    subparsers = parser.add_subparsers(title="Subcommands", required=True)

    cat_parser = subparsers.add_parser(
        "cat",
        help="Print the contents of a file",
        description="Print the contents of a file to stdout",
    )
    cat_parser.add_argument("src", nargs="+", help="File pattern to print")
    cat_parser.set_defaults(func=cat)

    chmod_parser = subparsers.add_parser(
        "chmod",
        help="Changes permissions of a file",
        description="Changes permissions of a file. Only octal permissions are supported.",
    )
    chmod_parser.add_argument(
        "-R",
        "--recursive",
        action="store_true",
        help="Modify files recursively",
    )
    chmod_parser.add_argument(
        "octalmode",
        help="The mode to set the permission to in octal format",
    )
    chmod_parser.add_argument("path", nargs="+", help="File pattern to update")
    chmod_parser.set_defaults(func=chmod)

    chown_parser = subparsers.add_parser(
        "chown",
        help="Changes owner and group of a file",
        description="Changes owner and group of a file",
    )
    chown_parser.add_argument(
        "-R",
        "--recursive",
        action="store_true",
        help="Modify files recursively",
    )
    chown_parser.add_argument(
        "owner",
        metavar="[owner[:group] | :group]",
        help="Owner and group to set for the file",
    )
    chown_parser.add_argument("path", nargs="+", help="File pattern to modify")
    chown_parser.set_defaults(func=chown)

    get_parser = subparsers.add_parser(
        "get",
        aliases=["copyToLocal"],
        help="Copy files to a local destination",
        description="""Copy files matching a pattern to a local destination.
            When copying multiple files, the destination must be a directory""",
    )
    get_parser.add_argument(
        "-p",
        "--preserve",
        action="store_true",
        default=False,
        help="Preserve timestamps and the mode",
    )
    get_parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        default=False,
        help="Overwrite the destination if it already exists",
    )
    get_parser.add_argument(
        "-t",
        "--threads",
        type=int,
        help="Number of threads to use",
        default=1,
    )
    get_parser.add_argument(
        "src",
        nargs="+",
        help="Source patterns to copy",
    )
    get_parser.add_argument(
        "localdst",
        help="Local destination to write to",
    )
    get_parser.set_defaults(func=get)

    mkdir_parser = subparsers.add_parser(
        "mkdir",
        help="Create a directory",
        description="Create a directory in a specified path",
    )
    mkdir_parser.add_argument(
        "path",
        nargs="+",
        help="Path for the directory to create",
    )
    mkdir_parser.add_argument(
        "-p",
        "--parent",
        action="store_true",
        help="Create any missing parent directories",
    )
    mkdir_parser.set_defaults(func=mkdir)

    mv_parser = subparsers.add_parser(
        "mv",
        help="Move files or directories",
        description="""Move a file or directory from <src> to <dst>. Must be part of the same name service.
        If multiple src are provided, dst must be a directory""",
    )
    mv_parser.add_argument("src", nargs="+", help="Files or directories to move")
    mv_parser.add_argument("dst", help="Target destination of file or directory")
    mv_parser.set_defaults(func=mv)

    args = parser.parse_args(in_args)
    args.func(args)


if __name__ == "__main__":
    main()
