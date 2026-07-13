import argparse
import asyncio

async def run(user_id: str, category: str, *, sandbox: bool) -> bool:
    """Retire the untracked manual sticker sender.

    A local image upload is an externally visible expression.  It cannot be
    honestly represented as a World delivery when this command has neither a
    World-authorized sticker Action nor a stable conversational causation
    record.  In particular, treating an HTTP coroutine return as proof of a
    share would recreate the old direct-send/implicit-confirm bypass.

    Keep the console entry point as a clear migration notice instead of
    silently deleting an operator command.  Stickers selected during a normal
    turn are scheduled by the World and settled by ``CompanionTurn`` through
    the platform receipt seam.
    """
    # Do not even construct the runtime: that avoids opening a model client or
    # loading mutable state for a command that is forbidden to produce a side
    # effect.  The legacy runtime is equally unsupported because it cannot
    # settle an unknown image receipt either.
    del user_id, category, sandbox
    print(
        "not sent: companion-send-sticker is retired; manual sticker uploads "
        "bypass World authorization and receipt settlement. Send through a "
        "World-backed companion turn instead."
    )
    return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Retired: manual sticker uploads bypass World delivery settlement."
    )
    parser.add_argument("--user", default="geoff", help="Canonical user id.")
    parser.add_argument("--category", default="happy", help="Sticker category.")
    parser.add_argument("--sandbox", action="store_true", help="Use QQ sandbox API.")
    args = parser.parse_args()
    asyncio.run(run(args.user, args.category, sandbox=args.sandbox))
