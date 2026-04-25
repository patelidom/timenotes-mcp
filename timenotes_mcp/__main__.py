from ._secrets import load_secrets
from .server import auto_login_from_env, mcp


def main() -> None:
    load_secrets()
    auto_login_from_env()
    mcp.run()


if __name__ == "__main__":
    main()
