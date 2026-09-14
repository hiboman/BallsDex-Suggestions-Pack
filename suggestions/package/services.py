from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import discord
from asgiref.sync import sync_to_async
from django.core.files.base import ContentFile
from django.db import IntegrityError
from django.db.models import Count
from PIL import Image

from ballsdex.core.utils.checks import get_user_for_check
from bd_models.models import Ball, BallGroup, Economy, Player, Regime, Special
from settings.models import settings

from ..models import Suggestion, SuggestionComment, SuggestionVote

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from ballsdex.core.bot import BallsDexBot

PER_PAGE = 5


class SuggestionApprovalError(Exception):
    """
    Raised by `SuggestionsService.approve_and_create` when a suggestion can't actually be turned
    into a real BallsDex object yet (missing required art, a name collision, etc). The message is
    written to be shown to staff as-is.
    """


EMOJI_MAX_BYTES = 256 * 1024
EMOJI_THUMBNAIL_SIZE = (128, 128)
EMOJI_NAME_MAX_LENGTH = 32


def _read_field_file(field_file) -> bytes:
    """Synchronous helper for reading a Django `FieldFile`'s bytes off of storage."""
    field_file.open("rb")
    try:
        return field_file.read()
    finally:
        field_file.close()


async def _read_art_bytes(field_file) -> bytes:
    """Reads a suggestion art `FieldFile`'s bytes, offloaded via `sync_to_async` since the actual
    storage read is blocking."""
    return await sync_to_async(_read_field_file)(field_file)


async def _copy_art(field_file) -> ContentFile:
    """
    Copies an already-uploaded suggestion art field into a fresh `ContentFile`, suitable for
    assigning to a brand new model's `ImageField` (approving a suggestion shouldn't leave the new
    `Ball`/`Regime`/`Special`/`Economy` row pointing at the suggestion's own storage path).
    """
    name = field_file.name.rsplit("/", 1)[-1]
    data = await _read_art_bytes(field_file)
    return ContentFile(data, name)


def _build_emoji_bytes(source_bytes: bytes) -> bytes:
    """
    Downscales full-size Ball art into something small enough to upload as a Discord application
    emoji (Discord caps emoji uploads at 256KB, and spawn art is easily megabytes). Pure CPU work,
    meant to be run via `asyncio.to_thread`.
    """
    with Image.open(io.BytesIO(source_bytes)) as image:
        image = image.convert("RGBA")
        image.thumbnail(EMOJI_THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        data = buffer.getvalue()
    if len(data) > EMOJI_MAX_BYTES:
        with Image.open(io.BytesIO(source_bytes)) as image:
            image = image.convert("RGBA")
            image.thumbnail((64, 64), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG", optimize=True)
            data = buffer.getvalue()
    return data


async def _create_emoji_for(bot: "BallsDexBot", suggestion: Suggestion) -> tuple[int, str | None]:
    """
    Uploads a small thumbnail as a Discord application emoji, named after the ball's full name, and
    returns its ID - so approving a Countryball suggestion doesn't need staff to have already
    uploaded an emoji anywhere themselves. Uses the suggestion's own `emoji_art` if the suggester
    uploaded one (see `NamesModal` in `ui.py`); otherwise falls back to a thumbnail of `spawn_art`,
    same as before that field existed.

    Returns `(emoji_id, warning)`. If the name doesn't fit Discord's 32-character emoji name limit,
    or Discord otherwise rejects the upload, this deliberately does NOT try to mangle the name into
    something that fits: it comes back as `(0, warning)` instead, leaving `emoji_id` as an obvious
    placeholder for staff to replace by hand once they've sorted out a real emoji.
    """
    name = suggestion.name.strip().replace(" ", "_")
    if len(name) > EMOJI_NAME_MAX_LENGTH:
        return 0, (
            f"**{suggestion.name}** is too long for a Discord emoji name ({EMOJI_NAME_MAX_LENGTH} characters "
            "max), so no emoji was created. Set `emoji_id` on the new Ball manually once you've uploaded one."
        )

    source_bytes = await _read_art_bytes(suggestion.emoji_art if suggestion.emoji_art else suggestion.spawn_art)
    emoji_bytes = await asyncio.to_thread(_build_emoji_bytes, source_bytes)
    try:
        emoji = await bot.create_application_emoji(name=name, image=emoji_bytes)
    except discord.HTTPException as error:
        return 0, (
            f"Discord rejected the emoji upload for **{suggestion.name}** ({error}), so no emoji was created. "
            "Set `emoji_id` on the new Ball manually once you've uploaded one."
        )
    bot.application_emojis[emoji.id] = emoji
    return emoji.id, None


async def _delete_emoji_for(bot: "BallsDexBot", emoji_id: int) -> None:
    """
    Deletes a Ball's existing Discord application emoji, used when approving a Countryball revamp:
    the revamp always uploads a fresh emoji from its new spawn art (see `_create_emoji_for`), and
    the old one otherwise just sits there unused forever. Best-effort - a missing/already-deleted
    emoji, or any Discord error, is silently ignored rather than blocking the revamp's approval
    over a leftover emoji staff can clean up by hand.
    """
    if not emoji_id:
        return
    emoji = bot.application_emojis.get(emoji_id)
    if emoji is None:
        try:
            emoji = await bot.fetch_application_emoji(emoji_id)
        except discord.HTTPException:
            return
    try:
        await emoji.delete()
    except discord.HTTPException:
        pass
    bot.application_emojis.pop(emoji_id, None)


@dataclass
class SuggestionDraft:
    """
    Accumulates the data collected across the steps of a suggestion form (Countryball, Card, or
    Economy - see `ui.py`). A single instance is threaded through every modal of one submission.

    `edit_pk` is set when this draft is editing an existing suggestion rather than creating a new
    one (see `SuggestionsService.update_suggestion`); the `*_bytes` fields are then allowed to
    stay empty, meaning "keep the existing art". `type`/`card_type` are fixed for the whole form
    (they decide which modals are shown in the first place) and never change on an edit.

    `is_revamp`/`revamp_*_id` are set when this draft proposes replacing an existing real
    object's data rather than creating a new one (picked via the search flow in `ui.py` before the
    first modal opens); like `type`/`card_type`, they're fixed for the whole form. Exactly one of
    the four `revamp_*_id` fields is ever set, matching whichever real model `type`/`card_type`
    targets.
    """

    edit_pk: int | None = None

    type: str = Suggestion.Type.COUNTRYBALL
    card_type: str | None = None

    is_revamp: bool = False
    revamp_ball_id: int | None = None
    revamp_regime_id: int | None = None
    revamp_special_id: int | None = None
    revamp_economy_id: int | None = None

    name: str = ""
    short_name: str | None = None
    catch_names: str | None = None
    rarity: float | None = None
    credits: str | None = None

    regime_id: int | None = None
    economy_id: int | None = None
    group_id: int | None = None
    health: int | None = None
    attack: int | None = None
    capacity_name: str | None = None
    capacity_description: str | None = None

    catch_phrase: str | None = None
    emoji: str | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None

    spawn_filename: str = ""
    spawn_bytes: bytes = b""
    window_filename: str = ""
    window_bytes: bytes = b""
    emoji_filename: str = ""
    emoji_bytes: bytes = b""

    card_background_filename: str = ""
    card_background_bytes: bytes = b""

    economy_icon_filename: str = ""
    economy_icon_bytes: bytes = b""


class SuggestionsService:
    """
    All database access for the suggestions package, kept separate from the Discord UI layer
    (`ui.py`) so the two can be read/tested/changed independently. Suggestions are a single
    global list (not scoped per-server) - see `models.Suggestion`.
    """

    def get_queryset(
        type: str | None = None, is_revamp: bool = False, sort: str = "creation_date"
    ) -> "QuerySet[Suggestion]":
        """
        `is_revamp` splits browsing into two entirely separate queues per `type` - revamp and
        "new object" suggestions never mix in the same list (see the `revamp` parameter on
        `/suggestions` in `cog.py`), even though they share every other field on this model.

        `sort` picks how that list is ordered - `"creation_date"` (newest first, the default) or
        `"upvotes"` (most-upvoted first, ties broken alphabetically by name - so of two
        suggestions tied at 41 upvotes, "Apple" sorts above "Banana") - see the `sort` parameter
        on `/suggestions` in `cog.py`.
        """
        order_by = ("-vote_count", "name") if sort == "upvotes" else ("-created_at",)
        queryset = (
            Suggestion.objects.select_related(
                "regime", "economy", "group", "submitted_by", "revamp_ball", "revamp_regime",
                "revamp_special", "revamp_economy",
            )
            .annotate(vote_count=Count("votes"))
            .filter(is_revamp=is_revamp)
            .order_by(*order_by)
        )
        if type is not None:
            queryset = queryset.filter(type=type)
        return queryset

    async def get_detail(pk: int) -> Suggestion:
        return await (
            Suggestion.objects.select_related(
                "regime", "economy", "group", "submitted_by", "revamp_ball", "revamp_regime",
                "revamp_special", "revamp_economy", "revamp_ball__regime", "revamp_ball__economy",
            )
            .prefetch_related("revamp_ball__groups")
            .annotate(
                vote_count=Count("votes", distinct=True),
                comment_count=Count("comments", distinct=True),
            )
            .aget(pk=pk)
        )

    async def get_status(suggestion_id: int) -> str:
        """
        Just the current `status` of a suggestion, without the heavier joins/annotations
        `get_detail` does. Used to re-check live whether a suggestion is still open before letting
        someone act on it (e.g. `CommentsView`/`AddCommentModal` re-checking this isn't Approved
        right before actually posting a comment), rather than trusting state a view might have
        loaded a while ago. Raises `Suggestion.DoesNotExist` if the suggestion was deleted.
        """
        return await Suggestion.objects.values_list("status", flat=True).aget(pk=suggestion_id)

    async def get_art_files(suggestion: Suggestion) -> list[tuple[discord.File, str]]:
        """
        Reads the suggestion's relevant art field(s) (depends on `type`) into fresh `discord.File`
        objects, paired with a human label for each - ready to be attached directly to the Discord
        message that renders the suggestion (see `SuggestionsView._load_art` in `package/ui.py`).

        Attaching the files directly, rather than linking to wherever the bot's web server happens
        to serve `MEDIA_URL`, means the art always renders regardless of whether that's even
        publicly reachable - Discord's servers only ever need to fetch it from Discord's own CDN.
        """
        if suggestion.type == Suggestion.Type.COUNTRYBALL:
            fields = [
                (suggestion.spawn_art, "spawn", "Spawn Art"),
                (suggestion.window_art, "window", "Window Art"),
                (suggestion.emoji_art, "emoji", "Emoji Art"),
            ]
        elif suggestion.type == Suggestion.Type.CARD:
            fields = [(suggestion.card_background, "background", "Background Art")]
        else:
            fields = [(suggestion.economy_icon, "icon", "Icon")]

        results: list[tuple[discord.File, str]] = []
        for field_file, slug, description in fields:
            if not field_file:
                continue
            data = await _read_art_bytes(field_file)
            extension = field_file.name.rsplit(".", 1)[-1] if "." in field_file.name else "png"
            file = discord.File(io.BytesIO(data), filename=f"{slug}.{extension}")
            results.append((file, description))
        return results

    async def has_voted(suggestion_id: int, discord_id: int) -> bool:
        """
        Whether the player with this Discord ID has voted on this suggestion. Filters by
        `discord_id` directly (rather than requiring a `Player` instance) so simply checking this
        - e.g. while rendering the menu for whoever last interacted with it - never has the side
        effect of creating a `Player` row for someone who has never actually voted.
        """
        return await SuggestionVote.objects.filter(
            suggestion_id=suggestion_id, player__discord_id=discord_id
        ).aexists()

    async def toggle_vote(suggestion_id: int, discord_id: int) -> tuple[bool, int]:
        """
        Adds or removes a vote from the player with this Discord ID on the given suggestion,
        whichever applies. Returns a `(voted, new_count)` tuple: `voted` is whether the player now
        has a vote on it, and `new_count` is the suggestion's total vote count after the change.
        """
        deleted, _ = await SuggestionVote.objects.filter(
            suggestion_id=suggestion_id, player__discord_id=discord_id
        ).adelete()
        if not deleted:
            player, _ = await Player.objects.aget_or_create(discord_id=discord_id)
            await SuggestionVote.objects.acreate(suggestion_id=suggestion_id, player=player)
        new_count = await SuggestionVote.objects.filter(suggestion_id=suggestion_id).acount()
        return (not bool(deleted), new_count)

    async def set_status(
        suggestion_id: int, status: Suggestion.Status, admin_comment: str | None = None
    ) -> Suggestion:
        suggestion = await Suggestion.objects.aget(pk=suggestion_id)
        suggestion.status = status
        if admin_comment is not None:
            suggestion.admin_comment = admin_comment
        await suggestion.asave()
        return suggestion

    async def approve_and_create(
        bot: "BallsDexBot", suggestion_id: int, *, enabled: bool | None = None
    ) -> tuple[Suggestion, str | None]:
        """
        Approves a suggestion. For a normal suggestion this creates the real
        `Ball`/`Regime`/`Special`/`Economy` row it proposes, so staff no longer have to copy the
        fields over by hand in the admin. For a **revamp** suggestion (`is_revamp`), it instead
        overwrites the existing real object it targeted (`revamp_ball`/`revamp_regime`/
        `revamp_special`/`revamp_economy`) with the suggested data - a revamped Countryball also
        gets a brand new Discord application emoji, with its old one deleted first (see
        `_delete_emoji_for`/`_create_emoji_for`), since the old emoji was made from art this
        suggestion is replacing. Idempotent: calling this again on an already-approved suggestion
        just returns it as-is, rather than creating/overwriting a second time.

        `enabled` only applies to a brand new (non-revamp) Countryball suggestion - see
        `SuggestionsView._on_approve` in `ui.py`, which prompts staff for it before calling this.
        Left as `None` (the default for every other case: Card, Economy, and any revamp), the new
        `Ball` keeps its normal model default (`enabled=True`) rather than being forced one way.

        Returns `(suggestion, warning)`. `warning` is only ever set for a Countryball
        creation/revamp whose emoji upload didn't happen (see `_create_emoji_for`) - the Ball is
        still created/updated, just with `emoji_id=0` as an obvious placeholder, and staff are
        expected to sort out a real emoji themselves afterwards.

        Raises `SuggestionApprovalError` (safe to show directly to staff) if the suggestion can't be
        approved at all yet - missing required art on a fresh suggestion, no regime set on a fresh
        Countryball, no revamp target selected, or a name collision with an existing object.
        """
        suggestion = await Suggestion.objects.select_related(
            "regime", "economy", "group", "revamp_ball", "revamp_regime", "revamp_special", "revamp_economy"
        ).aget(pk=suggestion_id)
        if suggestion.status == Suggestion.Status.APPROVED:
            return suggestion, None

        warning: str | None = None
        try:
            if suggestion.type == Suggestion.Type.COUNTRYBALL:
                if suggestion.is_revamp and suggestion.revamp_ball_id is None:
                    raise SuggestionApprovalError(
                        f"This revamp suggestion no longer has a {settings.collectible_name.title()} to revamp "
                        "(it may have been deleted) and can't be approved yet."
                    )
                if not suggestion.is_revamp:
                    if not suggestion.spawn_art or not suggestion.window_art:
                        raise SuggestionApprovalError(
                            "This suggestion is missing its spawn/window art and can't be approved yet."
                        )
                    if suggestion.regime_id is None:
                        raise SuggestionApprovalError(
                            "This suggestion has no regime set and can't be approved yet."
                        )
                if len(suggestion.name) > 48:
                    raise SuggestionApprovalError(
                        f"This suggestion's name is too long for a real {settings.collectible_name.title()} "
                        "(48 characters max)."
                    )

                if suggestion.is_revamp:
                    ball = suggestion.revamp_ball
                    if suggestion.spawn_art or suggestion.emoji_art:
                        await _delete_emoji_for(bot, ball.emoji_id)
                        emoji_id, warning = await _create_emoji_for(bot, suggestion)
                        ball.emoji_id = emoji_id
                    if suggestion.spawn_art:
                        ball.wild_card = await _copy_art(suggestion.spawn_art)
                    if suggestion.window_art:
                        ball.collection_card = await _copy_art(suggestion.window_art)
                    ball.country = suggestion.name
                    if suggestion.health is not None:
                        ball.health = suggestion.health
                    if suggestion.attack is not None:
                        ball.attack = suggestion.attack
                    if suggestion.rarity is not None:
                        ball.rarity = suggestion.rarity
                    if suggestion.credits:
                        ball.credits = suggestion.credits
                    if suggestion.capacity_name:
                        ball.capacity_name = suggestion.capacity_name
                    if suggestion.capacity_description:
                        ball.capacity_description = suggestion.capacity_description
                    if suggestion.short_name:
                        ball.short_name = suggestion.short_name
                    if suggestion.catch_names:
                        ball.catch_names = suggestion.catch_names
                    if suggestion.economy_id is not None:
                        ball.economy_id = suggestion.economy_id
                    if suggestion.regime_id is not None:
                        ball.regime_id = suggestion.regime_id
                    await ball.asave()
                    if suggestion.group_id is not None:
                        await ball.groups.aclear()
                        await ball.groups.aadd(suggestion.group)
                else:
                    ball = Ball()
                    if enabled is not None:
                        ball.enabled = enabled
                    emoji_id, warning = await _create_emoji_for(bot, suggestion)
                    ball.emoji_id = emoji_id
                    ball.wild_card = await _copy_art(suggestion.spawn_art)
                    ball.collection_card = await _copy_art(suggestion.window_art)
                    ball.country = suggestion.name
                    ball.health = suggestion.health or 0
                    ball.attack = suggestion.attack or 0
                    ball.rarity = suggestion.rarity or 0
                    ball.credits = suggestion.credits or ""
                    ball.capacity_name = suggestion.capacity_name or ""
                    ball.capacity_description = suggestion.capacity_description or ""
                    ball.short_name = suggestion.short_name
                    ball.catch_names = suggestion.catch_names
                    ball.economy_id = suggestion.economy_id
                    ball.regime_id = suggestion.regime_id
                    await ball.asave()
                    if suggestion.group_id is not None:
                        await ball.groups.aadd(suggestion.group)

            elif suggestion.type == Suggestion.Type.CARD:
                if suggestion.is_revamp:
                    if (
                        suggestion.card_type == Suggestion.CardType.REGIME
                        and suggestion.revamp_regime_id is None
                    ):
                        raise SuggestionApprovalError(
                            "This revamp suggestion no longer has a Regime to revamp (it may have "
                            "been deleted) and can't be approved yet."
                        )
                    if (
                        suggestion.card_type != Suggestion.CardType.REGIME
                        and suggestion.revamp_special_id is None
                    ):
                        raise SuggestionApprovalError(
                            "This revamp suggestion no longer has a Special event to revamp (it may "
                            "have been deleted) and can't be approved yet."
                        )
                elif not suggestion.card_background:
                    raise SuggestionApprovalError(
                        "This suggestion is missing its background art and can't be approved yet."
                    )

                if suggestion.card_type == Suggestion.CardType.REGIME:
                    regime = suggestion.revamp_regime if suggestion.is_revamp else Regime()
                    regime.name = suggestion.name
                    if suggestion.card_background:
                        regime.background = await _copy_art(suggestion.card_background)
                    await regime.asave()
                else:
                    special = suggestion.revamp_special if suggestion.is_revamp else Special()
                    special.name = suggestion.name
                    if suggestion.card_background:
                        special.background = await _copy_art(suggestion.card_background)
                    if suggestion.is_revamp:
                        if suggestion.catch_phrase:
                            special.catch_phrase = suggestion.catch_phrase
                        if suggestion.emoji:
                            special.emoji = suggestion.emoji
                        if suggestion.start_date is not None:
                            special.start_date = suggestion.start_date
                        if suggestion.end_date is not None:
                            special.end_date = suggestion.end_date
                        if suggestion.rarity is not None:
                            special.rarity = suggestion.rarity
                        if suggestion.credits:
                            special.credits = suggestion.credits
                    else:
                        special.catch_phrase = suggestion.catch_phrase
                        special.emoji = suggestion.emoji
                        special.start_date = suggestion.start_date
                        special.end_date = suggestion.end_date
                        special.rarity = suggestion.rarity or 0
                        special.credits = suggestion.credits
                    await special.asave()

            else:  # Suggestion.Type.ECONOMY
                if suggestion.is_revamp:
                    if suggestion.revamp_economy_id is None:
                        raise SuggestionApprovalError(
                            "This revamp suggestion no longer has an Economy to revamp (it may have been "
                            "deleted) and can't be approved yet."
                        )
                    economy = suggestion.revamp_economy
                else:
                    if not suggestion.economy_icon:
                        raise SuggestionApprovalError(
                            "This suggestion is missing its icon art and can't be approved yet."
                        )
                    economy = Economy()
                economy.name = suggestion.name
                if suggestion.economy_icon:
                    economy.icon = await _copy_art(suggestion.economy_icon)
                await economy.asave()
        except IntegrityError as error:
            raise SuggestionApprovalError(
                f"Couldn't save the real object - something with this name may already exist ({error})."
            ) from error

        suggestion.status = Suggestion.Status.APPROVED
        await suggestion.asave()
        return suggestion, warning

    async def is_staff(bot: "BallsDexBot", user: "discord.abc.User") -> bool:
        """
        Whether `user` may approve/deny/request changes on suggestions: bot owners, Django
        superusers, and Django staff users all qualify.
        """
        result = await get_user_for_check(bot, user)
        if isinstance(result, bool):
            return result
        return result.is_staff

    def get_comments_queryset(suggestion_id: int) -> "QuerySet[SuggestionComment]":
        return (
            SuggestionComment.objects.select_related("author")
            .filter(suggestion_id=suggestion_id)
            .order_by("created_at")
        )

    async def add_comment(suggestion_id: int, discord_user_id: int, content: str) -> SuggestionComment:
        player, _ = await Player.objects.aget_or_create(discord_id=discord_user_id)
        return await SuggestionComment.objects.acreate(suggestion_id=suggestion_id, author=player, content=content)

    async def list_regimes() -> list[Regime]:
        return [regime async for regime in Regime.objects.all()]

    async def list_economies() -> list[Economy]:
        return [economy async for economy in Economy.objects.all()]

    async def list_groups() -> list[BallGroup]:
        return [group async for group in BallGroup.objects.all()]


    async def get_ball_by_exact_name(name: str) -> Ball | None:
        return await Ball.objects.filter(country__iexact=name).afirst()

    async def get_regime_by_exact_name(name: str) -> Regime | None:
        return await Regime.objects.filter(name__iexact=name).afirst()

    async def get_special_by_exact_name(name: str) -> Special | None:
        return await Special.objects.filter(name__iexact=name).afirst()

    async def get_economy_by_exact_name(name: str) -> Economy | None:
        return await Economy.objects.filter(name__iexact=name).afirst()

    async def get_revamp_target(draft: SuggestionDraft) -> Ball | Regime | Special | Economy | None:
        if draft.type == Suggestion.Type.COUNTRYBALL:
            if draft.revamp_ball_id is None:
                return None
            return await Ball.objects.filter(pk=draft.revamp_ball_id).afirst()
        if draft.type == Suggestion.Type.CARD:
            if draft.card_type == Suggestion.CardType.REGIME:
                if draft.revamp_regime_id is None:
                    return None
                return await Regime.objects.filter(pk=draft.revamp_regime_id).afirst()
            if draft.revamp_special_id is None:
                return None
            return await Special.objects.filter(pk=draft.revamp_special_id).afirst()
        if draft.revamp_economy_id is None:
            return None
        return await Economy.objects.filter(pk=draft.revamp_economy_id).afirst()

    async def create_suggestion(*, discord_user_id: int, draft: SuggestionDraft) -> Suggestion:
        player, _ = await Player.objects.aget_or_create(discord_id=discord_user_id)
        return await Suggestion.objects.acreate(
            submitted_by=player,
            type=draft.type,
            card_type=draft.card_type,
            is_revamp=draft.is_revamp,
            revamp_ball_id=draft.revamp_ball_id,
            revamp_regime_id=draft.revamp_regime_id,
            revamp_special_id=draft.revamp_special_id,
            revamp_economy_id=draft.revamp_economy_id,
            name=draft.name,
            short_name=draft.short_name,
            catch_names=draft.catch_names,
            regime_id=draft.regime_id,
            economy_id=draft.economy_id,
            group_id=draft.group_id,
            health=draft.health,
            attack=draft.attack,
            rarity=draft.rarity,
            credits=draft.credits,
            capacity_name=draft.capacity_name,
            capacity_description=draft.capacity_description,
            catch_phrase=draft.catch_phrase,
            emoji=draft.emoji,
            start_date=draft.start_date,
            end_date=draft.end_date,
            spawn_art=ContentFile(draft.spawn_bytes, draft.spawn_filename) if draft.spawn_bytes else None,
            window_art=(
                ContentFile(draft.window_bytes, draft.window_filename) if draft.window_bytes else None
            ),
            emoji_art=(ContentFile(draft.emoji_bytes, draft.emoji_filename) if draft.emoji_bytes else None),
            card_background=(
                ContentFile(draft.card_background_bytes, draft.card_background_filename)
                if draft.card_background_bytes
                else None
            ),
            economy_icon=(
                ContentFile(draft.economy_icon_bytes, draft.economy_icon_filename)
                if draft.economy_icon_bytes
                else None
            ),
        )

    async def update_suggestion(pk: int, draft: SuggestionDraft) -> Suggestion:
        """
        Applies an edited draft onto an existing suggestion (see the "Edit Submission" flow in
        `ui.py`). Like a GitHub pull request, pushing an edit does *not* touch `status` or
        `admin_comment` - whatever a staff member last set (Pending, Approved, Rejected, or
        Changes Requested with its note) stays put until they explicitly act again via the
        Approve/Deny/Request Changes buttons, even though the underlying content just changed.
        `type`/`card_type` are never changed by an edit either. Art is only replaced if new bytes
        were provided - re-uploading isn't required to edit the other fields. Fields that don't
        apply to this suggestion's type are simply re-written with their (already-empty) draft
        value, which is a no-op.
        """
        suggestion = await Suggestion.objects.aget(pk=pk)
        suggestion.name = draft.name
        suggestion.short_name = draft.short_name
        suggestion.catch_names = draft.catch_names
        suggestion.regime_id = draft.regime_id
        suggestion.economy_id = draft.economy_id
        suggestion.group_id = draft.group_id
        suggestion.health = draft.health
        suggestion.attack = draft.attack
        suggestion.rarity = draft.rarity
        suggestion.credits = draft.credits
        suggestion.capacity_name = draft.capacity_name
        suggestion.capacity_description = draft.capacity_description
        suggestion.catch_phrase = draft.catch_phrase
        suggestion.emoji = draft.emoji
        suggestion.start_date = draft.start_date
        suggestion.end_date = draft.end_date
        if draft.spawn_bytes:
            suggestion.spawn_art = ContentFile(draft.spawn_bytes, draft.spawn_filename)
        if draft.window_bytes:
            suggestion.window_art = ContentFile(draft.window_bytes, draft.window_filename)
        if draft.emoji_bytes:
            suggestion.emoji_art = ContentFile(draft.emoji_bytes, draft.emoji_filename)
        if draft.card_background_bytes:
            suggestion.card_background = ContentFile(draft.card_background_bytes, draft.card_background_filename)
        if draft.economy_icon_bytes:
            suggestion.economy_icon = ContentFile(draft.economy_icon_bytes, draft.economy_icon_filename)
        await suggestion.asave()
        return suggestion
