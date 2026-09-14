from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Callable

import discord
from discord import ButtonStyle, MediaGalleryItem, SelectOption
from discord.ui import (
    ActionRow,
    Button,
    Container,
    FileUpload,
    Label,
    MediaGallery,
    Select,
    Separator,
    TextDisplay,
    TextInput,
)
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from ballsdex.core.discord import LayoutView, Modal, View
from ballsdex.core.utils.buttons import ConfirmChoiceView
from ballsdex.core.utils.menus import Formatter, Menu, ModelSource
from bd_models.models import BallGroup, Economy, Regime
from settings.models import settings

from ..models import Suggestion, SuggestionComment
from .services import PER_PAGE, SuggestionApprovalError, SuggestionDraft, SuggestionsService

if TYPE_CHECKING:
    from datetime import datetime

    from django.db.models import QuerySet

    from ballsdex.core.bot import BallsDexBot

type Interaction = discord.Interaction["BallsDexBot"]

log = logging.getLogger("ballsdex.packages.suggestions")

NO_SUGGESTION_VALUE = "_none"


def _forget_dispatch(view: "LayoutView", item: "Button | Select") -> None:
    state = getattr(view.bot, "_connection", None)
    view_store = getattr(state, "_view_store", None)
    if view_store is None:
        return
    dispatch_info = view_store._views.get(view._cache_key)
    if dispatch_info is not None:
        dispatch_info.pop((item.type.value, item.custom_id), None)


MAX_IMAGE_SIZE = 20 * 1024 * 1024

TYPE_LABELS = {
    Suggestion.Type.CARD: "Cards",
    Suggestion.Type.ECONOMY: "Economies",
}

STATUS_ACCENT_COLOURS: dict[str, discord.Colour] = {
    Suggestion.Status.APPROVED: discord.Colour.green(),
    Suggestion.Status.PENDING: discord.Colour.light_grey(),
    Suggestion.Status.CHANGES_REQUESTED: discord.Colour.gold(),
    Suggestion.Status.REJECTED: discord.Colour.red(),
}


def _type_label(type: str) -> str:
    """
    Display name for a suggestion `type`. Countryball is the one kind whose name depends on the
    server's settings - a reskinned bot might call its collectible something else entirely - so
    it's computed here from `settings.plural_collectible_name` rather than baked into
    `TYPE_LABELS` above, which only ever needs the two names that never change.
    """
    if type == Suggestion.Type.COUNTRYBALL:
        return settings.plural_collectible_name.title()
    return TYPE_LABELS[type]

COMMENTS_PER_PAGE = 5
MAX_COMMENT_LENGTH = 1000


class ModelSourceAllowEmpty[M](ModelSource[M]):
    """
    `ModelSource.prepare()` raises `ValueError` when its queryset is empty - reasonable for a
    source that's always expected to have content, but not for us: an empty suggestion list or
    comment thread is a normal state (a brand new suggestion has no comments yet; a kind with no
    suggestions submitted shows the "No suggestions available" placeholder) that should render as
    one empty page instead of raising. Used by `SuggestionsView` and `CommentsView`.
    """

    async def prepare(self):
        try:
            await super().prepare()
        except ValueError:
            self.max = 1


def _quote_block(text: str) -> str:
    """Formats `text` as an italicized, quoted Discord blockquote (used in DMs)."""
    lines = text.splitlines() or [text]
    lines[0] = f'_"{lines[0]}'
    lines[-1] = f'{lines[-1]}"_'
    return "\n".join(f"> {line}" for line in lines)


def _parse_datetime(text: str) -> "datetime | None":
    """
    Parses a suggester-typed date like `2026-12-01 00:00` into an aware `datetime`, or returns
    `None` if it doesn't look like one (the caller turns that into a validation error).
    """
    dt = parse_datetime(text.strip())
    if dt is None:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.utc)
    return dt


async def _load_countryball_requirements() -> "tuple[list[Regime], list[Economy], str | None]":
    """
    Fetches the regimes/economies a Countryball suggestion needs to even be started, alongside a
    short reason if either is missing. Shared by the "New Suggestion"/"New Revamp" button (via
    `SuggestionsView._reload_data`, to decide whether it's disabled) and its click handlers
    (`SuggestionsView._on_new_suggestion`, `BallRevampSearchModal.on_submit`), which still re-check
    this themselves rather than trust the button's last-rendered state, in case an admin removed
    the last regime/economy in the time between that render and the click actually landing.
    """
    regimes = await SuggestionsService.list_regimes()
    if not regimes:
        return regimes, [], "No regimes exist."
    economies = await SuggestionsService.list_economies()
    if not economies:
        return regimes, economies, "No economies exist."
    return regimes, economies, None


def _validate_image(attachment: discord.Attachment) -> str | None:
    """
    Checks a suggester-uploaded image attachment against the type/size rules every image field in
    this package shares (Spawn/Window Art, Card background, Economy icon). Returns an error
    message to show the user, or `None` if the attachment is fine as-is.
    """
    if attachment.content_type and not attachment.content_type.startswith("image/"):
        return f"`{attachment.filename}` is not an image file."
    if attachment.size > MAX_IMAGE_SIZE:
        return f"`{attachment.filename}` is too large (max 20 MB)."
    return None


async def _can_edit(bot: "BallsDexBot", user: "discord.abc.User", suggestion: Suggestion) -> bool:
    """
    Whether `user` may edit `suggestion`: its own submitter, or any staff member (the "GitHub PR"
    model lets staff edit anyone's still-pending/denied suggestion). This is the one place that
    rule is actually enforced - `SuggestionsView`'s cached `viewer_can_edit` exists separately, only
    to decide whether to render the Edit button, and is explicitly not the security boundary (see
    the class docstring); `_on_edit_submission` calls this fresh, against whoever actually clicked,
    before allowing the edit itself.
    """
    if user.id == suggestion.submitted_by.discord_id:
        return True
    return await SuggestionsService.is_staff(bot, user)


def _add_exact_name_field(modal: Modal, *, label: str, description: str) -> TextInput:
    """
    Adds the single "type the exact name" text field shared by every revamp-search modal
    (`BallRevampSearchModal`, `CardRevampSearchModal`, `EconomyRevampSearchModal`) and returns it.
    Kept as a small helper rather than a shared base class, since a `discord.ui.Modal` subclass
    needs its own `title=` class kwarg and these three already differ enough elsewhere (Card's
    extra type select, Ball's dynamic title) that a full base class would save little beyond this
    one field.
    """
    name_input = TextInput(max_length=64)
    modal.add_item(Label(text=label, description=description, component=name_input))
    return name_input


_REVAMP_COMPARABLE_FIELDS: dict[tuple[str, str | None], tuple[tuple[str, str], ...]] = {
    (Suggestion.Type.COUNTRYBALL, None): (
        ("short_name", "short_name"),
        ("catch_names", "catch_names"),
        ("regime_id", "regime_id"),
        ("economy_id", "economy_id"),
        ("health", "health"),
        ("attack", "attack"),
        ("rarity", "rarity"),
        ("capacity_name", "capacity_name"),
        ("capacity_description", "capacity_description"),
        ("credits", "credits"),
    ),
    (Suggestion.Type.CARD, Suggestion.CardType.REGIME): (),
    (Suggestion.Type.CARD, Suggestion.CardType.SPECIAL): (
        ("catch_phrase", "catch_phrase"),
        ("emoji", "emoji"),
        ("start_date", "start_date"),
        ("end_date", "end_date"),
        ("rarity", "rarity"),
        ("credits", "credits"),
    ),
    (Suggestion.Type.ECONOMY, None): (),
}

_REVAMP_FILE_FIELDS: dict[tuple[str, str | None], tuple[str, ...]] = {
    (Suggestion.Type.COUNTRYBALL, None): ("spawn_bytes", "window_bytes", "emoji_bytes"),
    (Suggestion.Type.CARD, Suggestion.CardType.REGIME): ("card_background_bytes",),
    (Suggestion.Type.CARD, Suggestion.CardType.SPECIAL): ("card_background_bytes",),
    (Suggestion.Type.ECONOMY, None): ("economy_icon_bytes",),
}


async def _revamp_has_changes(draft: SuggestionDraft) -> bool:
    """
    Whether a revamp draft actually proposes changing something on the real object it targets. A
    field only counts if it's set AND different from the real object's current value - the same
    comparison `SuggestionsView._build_selected_text()` uses to decide what shows as "changed" to
    staff. An art/emoji upload always counts (comparing raw bytes against a stored file isn't
    worth doing here). If the target's been deleted since the suggester started, this lets it
    through - `approve_and_create` rejects it afterward with a clearer error.
    """
    key = (draft.type, draft.card_type if draft.type == Suggestion.Type.CARD else None)

    if any(getattr(draft, field) for field in _REVAMP_FILE_FIELDS[key]):
        return True

    target = await SuggestionsService.get_revamp_target(draft)
    if target is None:
        return True

    for draft_field, target_attr in _REVAMP_COMPARABLE_FIELDS[key]:
        value = getattr(draft, draft_field)
        if value and value != getattr(target, target_attr, None):
            return True

    if draft.type == Suggestion.Type.COUNTRYBALL and draft.group_id is not None:
        if not await target.groups.filter(pk=draft.group_id).aexists():
            return True

    return False


async def _finalize_suggestion(
    interaction: Interaction,
    draft: SuggestionDraft,
    existing: Suggestion | None,
    parent_view: "SuggestionsView | None",
) -> None:
    """
    Creates or updates the `Suggestion` row from a finished draft, refreshes the browsing menu
    that spawned this form (if any), and sends the final ephemeral confirmation. Shared by the
    last step of the Countryball, Card, and Economy forms - both fresh submissions and edits.
    `interaction.response` must already be deferred (ephemeral) by the caller before this runs,
    since it may need to read/upload file bytes.
    """
    if draft.is_revamp and not await _revamp_has_changes(draft):
        await interaction.followup.send(
            "You didn't propose any changes - every field is either blank or matches the current "
            "value. Fill in at least one field with something actually different before submitting.",
            ephemeral=True,
        )
        return

    if draft.edit_pk is not None:
        is_own_suggestion = existing is not None and existing.submitted_by.discord_id == interaction.user.id
        suggestion = await SuggestionsService.update_suggestion(draft.edit_pk, draft)
        log.info(
            f"{interaction.user} edited "
            f"{'their' if is_own_suggestion else 'someone else’s'} "
            f"{suggestion.get_type_display().lower()} suggestion: {suggestion.name!r}",
            extra={"webhook": True},
        )
        confirmation = (
            f"Your suggestion for **{suggestion.name}** was updated, thank you! Its status is unchanged - "
            "an admin will still need to Approve/Deny it (or request further changes)."
            if is_own_suggestion
            else f"**{suggestion.name}** was updated. Its status is unchanged."
        )
    else:
        suggestion = await SuggestionsService.create_suggestion(discord_user_id=interaction.user.id, draft=draft)
        log.info(
            f"{interaction.user} submitted a new {suggestion.get_type_display().lower()} suggestion: "
            f"{suggestion.name!r}",
            extra={"webhook": True},
        )
        confirmation = f"Your suggestion for **{suggestion.name}** was submitted, thank you for contributing!"

    if parent_view is not None:
        parent_view.viewing_user = interaction.user
        await parent_view._reload_data(keep_selection=True)
        if parent_view.original_message is not None:
            try:
                await parent_view.original_message.edit(view=parent_view, attachments=parent_view.current_files)
            except discord.NotFound:
                pass

    await interaction.followup.send(confirmation, ephemeral=True)


class ContinueView(View):
    """A single "Continue" button bridging two steps of a suggestion form."""

    def __init__(self, *, label: str, next_modal_factory: Callable[[], Modal]):
        super().__init__(timeout=None)
        self.next_modal_factory = next_modal_factory
        self.continue_button.label = label

    @discord.ui.button(style=ButtonStyle.blurple)
    async def continue_button(self, interaction: Interaction, button: Button):
        self.stop()
        await interaction.response.send_modal(self.next_modal_factory())




class BasicInfoModal(Modal, title="New Suggestion — Step 1/3"):
    def __init__(
        self,
        regimes: list[Regime],
        economies: list[Economy],
        *,
        revamp_ball_id: int | None = None,
        revamp_ball_name: str | None = None,
        existing: Suggestion | None = None,
        parent_view: "SuggestionsView | None" = None,
    ):
        revamp_ball_id = revamp_ball_id or (existing.revamp_ball_id if existing else None)
        is_revamp = revamp_ball_id is not None
        if existing is not None:
            super().__init__(title="Edit Suggestion — Step 1/3")
        elif is_revamp:
            super().__init__(title="Revamp Suggestion — Step 1/3")
        else:
            super().__init__()
        self.draft = SuggestionDraft(
            edit_pk=existing.pk if existing else None,
            type=Suggestion.Type.COUNTRYBALL,
            is_revamp=is_revamp,
            revamp_ball_id=revamp_ball_id,
        )
        self.existing = existing
        self.parent_view = parent_view
        self._blank_name_fallback = existing.name if existing is not None else revamp_ball_name

        self.name_input = TextInput(
            placeholder=f"Name of the {settings.collectible_name}",
            max_length=64,
            required=not is_revamp,
            default=existing.name if existing is not None else (revamp_ball_name if is_revamp else None),
        )
        self.add_item(
            Label(
                text="Full Name",
                description="Optional. Leave blank to keep the current name." if is_revamp else None,
                component=self.name_input,
            )
        )
        self.regime_select = Select(
            required=not is_revamp,
            min_values=0 if is_revamp else 1,
            max_values=1,
            options=[
                SelectOption(
                    label=r.name,
                    value=str(r.pk),
                    default=existing is not None and existing.regime_id == r.pk,
                )
                for r in regimes[:25]
            ],
        )
        self.add_item(
            Label(
                text="Regime",
                description="Optional. Leave blank to keep the current regime." if is_revamp else None,
                component=self.regime_select,
            )
        )

        self.economy_select = Select(
            required=not is_revamp,
            min_values=0 if is_revamp else 1,
            max_values=1,
            options=[
                SelectOption(
                    label=e.name,
                    value=str(e.pk),
                    default=existing is not None and existing.economy_id == e.pk,
                )
                for e in economies[:25]
            ],
        )
        self.add_item(
            Label(
                text="Economy",
                description="Optional. Leave blank to keep the current economy." if is_revamp else None,
                component=self.economy_select,
            )
        )

        hp_atk_default = f"{existing.health};{existing.attack}" if existing is not None else None
        self.hp_atk_input = TextInput(
            placeholder="Semicolon-separated stats", required=not is_revamp, default=hp_atk_default
        )
        self.add_item(
            Label(
                text="HP/ATK",
                description="Optional. Leave blank to keep the current stats." if is_revamp else None,
                component=self.hp_atk_input,
            )
        )

        self.rarity_input = TextInput(
            placeholder=f"Rarity of this {settings.collectible_name}",
            required=not is_revamp,
            default=str(existing.rarity) if existing is not None else None,
        )
        self.add_item(
            Label(
                text="Rarity",
                description="Optional. Leave blank to keep the current rarity." if is_revamp else None,
                component=self.rarity_input,
            )
        )

    async def on_submit(self, interaction: Interaction):
        is_revamp = self.draft.is_revamp
        hp_atk_value = self.hp_atk_input.value.strip()
        if hp_atk_value:
            hp_atk_parts = hp_atk_value.split(";")
            if len(hp_atk_parts) != 2:
                await interaction.response.send_message(
                    "HP/ATK must be two whole numbers separated by a semicolon, e.g. `2500;2500`.", ephemeral=True
                )
                return
            try:
                health = int(hp_atk_parts[0].strip())
                attack = int(hp_atk_parts[1].strip())
            except ValueError:
                await interaction.response.send_message(
                    "HP/ATK must be two whole numbers separated by a semicolon, e.g. `2500;2500`.", ephemeral=True
                )
                return
        elif is_revamp:
            health = None
            attack = None
        else:
            await interaction.response.send_message("You must set HP/ATK.", ephemeral=True)
            return

        rarity_value = self.rarity_input.value.strip()
        if rarity_value:
            try:
                rarity = float(rarity_value)
            except ValueError:
                await interaction.response.send_message("Rarity must be a number, e.g. `0.1`.", ephemeral=True)
                return
        elif is_revamp:
            rarity = None
        else:
            await interaction.response.send_message("Rarity must be a number, e.g. `0.1`.", ephemeral=True)
            return

        if not self.regime_select.values:
            if not is_revamp:
                await interaction.response.send_message("You must pick a regime.", ephemeral=True)
                return
            regime_id = None
        else:
            regime_id = int(self.regime_select.values[0])

        if not self.economy_select.values:
            if not is_revamp:
                await interaction.response.send_message("You must pick an economy.", ephemeral=True)
                return
            economy_id = None
        else:
            economy_id = int(self.economy_select.values[0])

        name = self.name_input.value.strip()
        if not name:
            if not is_revamp or not self._blank_name_fallback:
                await interaction.response.send_message("You must set a name.", ephemeral=True)
                return
            name = self._blank_name_fallback

        self.draft.name = name
        self.draft.regime_id = regime_id
        self.draft.economy_id = economy_id
        self.draft.health = health
        self.draft.attack = attack
        self.draft.rarity = rarity

        draft = self.draft
        existing = self.existing
        parent_view = self.parent_view
        view = ContinueView(
            label="Continue (2/3)",
            next_modal_factory=lambda: AbilityArtModal(draft, existing=existing, parent_view=parent_view),
        )
        await interaction.response.send_message(
            "Step 1/3 saved. Click below to continue with the artwork and emoji.",
            view=view,
            ephemeral=True,
        )


class AbilityArtModal(Modal, title="New Suggestion — Step 2/3"):
    def __init__(
        self,
        draft: SuggestionDraft,
        *,
        existing: Suggestion | None = None,
        parent_view: "SuggestionsView | None" = None,
    ):
        if existing is not None:
            super().__init__(title="Edit Suggestion — Step 2/3")
        else:
            super().__init__()
        self.draft = draft
        self.existing = existing
        self.parent_view = parent_view

        spawn_window_required = existing is None and not draft.is_revamp
        self.spawn_art_upload = FileUpload(max_values=1, required=spawn_window_required)
        self.add_item(
            Label(
                text="Spawn Art",
                description=(
                    f"Image used when a {settings.collectible_name} spawns."
                    if spawn_window_required
                    else "Optional. Leave blank to keep the current image."
                ),
                component=self.spawn_art_upload,
            )
        )

        self.window_art_upload = FileUpload(max_values=1, required=spawn_window_required)
        self.add_item(
            Label(
                text="Window Art",
                description=(
                    f"Image used when displaying {settings.plural_collectible_name}."
                    if spawn_window_required
                    else "Optional. Leave blank to keep the current image."
                ),
                component=self.window_art_upload,
            )
        )

        self.emoji_upload = FileUpload(max_values=1, required=False)
        self.add_item(
            Label(
                text="Emoji",
                description=(
                    "Optional. Leave blank to auto-generate the emoji from Spawn Art instead."
                    if existing is None
                    else "Optional. Leave blank to keep the current emoji."
                ),
                component=self.emoji_upload,
            )
        )

        self.credits_input = TextInput(
            placeholder="Author of the artwork",
            max_length=64,
            required=not draft.is_revamp,
            default=existing.credits if existing is not None else None,
        )
        self.add_item(Label(text="Credits", description="Artwork credits.", component=self.credits_input))

    async def on_submit(self, interaction: Interaction):
        spawn_files = self.spawn_art_upload.values
        window_files = self.window_art_upload.values
        emoji_files = self.emoji_upload.values

        if self.existing is None and not self.draft.is_revamp and (not spawn_files or not window_files):
            await interaction.response.send_message("Both images are required.", ephemeral=True)
            return

        for files in (spawn_files, window_files, emoji_files):
            if not files:
                continue
            if error := _validate_image(files[0]):
                await interaction.response.send_message(error, ephemeral=True)
                return

        credits = self.credits_input.value.strip()
        if not credits and not self.draft.is_revamp:
            await interaction.response.send_message("Credits are required.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        if credits:
            self.draft.credits = credits
        if spawn_files:
            self.draft.spawn_filename = spawn_files[0].filename
            self.draft.spawn_bytes = await spawn_files[0].read()
        if window_files:
            self.draft.window_filename = window_files[0].filename
            self.draft.window_bytes = await window_files[0].read()
        if emoji_files:
            self.draft.emoji_filename = emoji_files[0].filename
            self.draft.emoji_bytes = await emoji_files[0].read()

        groups = await SuggestionsService.list_groups()

        draft = self.draft
        existing = self.existing
        parent_view = self.parent_view
        view = ContinueView(
            label="Continue (3/3)",
            next_modal_factory=lambda: NamesModal(draft, groups, existing=existing, parent_view=parent_view),
        )
        await interaction.followup.send(
            "Step 2/3 saved. Click below to finish with the ability and a couple of extra names.",
            view=view,
            ephemeral=True,
        )


class NamesModal(Modal, title="New Suggestion — Step 3/3"):
    def __init__(
        self,
        draft: SuggestionDraft,
        groups: list[BallGroup],
        *,
        existing: Suggestion | None = None,
        parent_view: "SuggestionsView | None" = None,
    ):
        if existing is not None:
            super().__init__(title="Edit Suggestion — Step 3/3")
        else:
            super().__init__()
        self.draft = draft
        self.existing = existing
        self.parent_view = parent_view

        self.capacity_name_input = TextInput(
            placeholder=f"Name of the {settings.collectible_name}'s ability",
            max_length=64,
            required=not draft.is_revamp,
            default=existing.capacity_name if existing is not None else None,
        )
        self.add_item(
            Label(
                text="Ability",
                description="Optional. Leave blank to keep the current ability." if draft.is_revamp else None,
                component=self.capacity_name_input,
            )
        )

        self.capacity_description_input = TextInput(
            style=discord.TextStyle.paragraph,
            placeholder=f"Description of the {settings.collectible_name}'s ability",
            max_length=256,
            required=not draft.is_revamp,
            default=existing.capacity_description if existing is not None else None,
        )
        self.add_item(
            Label(
                text="Ability Description",
                description="Optional. Leave blank to keep the current description." if draft.is_revamp else None,
                component=self.capacity_description_input,
            )
        )

        self.short_name_input = TextInput(
            placeholder="A shorter name used only when generating the card.",
            max_length=24,
            required=False,
            default=existing.short_name if existing is not None else None,
        )
        self.add_item(
            Label(
                text="Short Name",
                description="Optional.",
                component=self.short_name_input,
            )
        )

        self.catch_names_input = TextInput(
            placeholder="Additional catch names, semicolon seperated.",
            required=False,
            max_length=200,
            default=existing.catch_names if existing is not None else None,
        )
        self.add_item(
            Label(
                text="Catch Names",
                description=f"Optional.",
                component=self.catch_names_input,
            )
        )

        self.group_select: Select | None = None
        if groups:
            self.group_select = Select(
                required=False,
                min_values=0,
                max_values=1,
                options=[
                    SelectOption(
                        label=g.name,
                        value=str(g.pk),
                        default=existing is not None and existing.group_id == g.pk,
                    )
                    for g in groups[:25]
                ],
            )
            self.add_item(
                Label(text="Group", description='Optional.', component=self.group_select)
            )

    async def on_submit(self, interaction: Interaction):
        capacity_name = self.capacity_name_input.value.strip()
        capacity_description = self.capacity_description_input.value.strip()
        if not self.draft.is_revamp and not capacity_name:
            await interaction.response.send_message("An ability name is required.", ephemeral=True)
            return
        if not self.draft.is_revamp and not capacity_description:
            await interaction.response.send_message("An ability description is required.", ephemeral=True)
            return

        self.draft.capacity_name = capacity_name or None
        self.draft.capacity_description = capacity_description or None
        self.draft.short_name = self.short_name_input.value.strip() or None
        self.draft.catch_names = self.catch_names_input.value.strip() or None
        if self.group_select is not None and self.group_select.values:
            self.draft.group_id = int(self.group_select.values[0])
        else:
            self.draft.group_id = None

        await interaction.response.defer(ephemeral=True, thinking=True)

        await _finalize_suggestion(interaction, self.draft, self.existing, self.parent_view)




class CardBasicModal(Modal, title="New Card Suggestion"):
    def __init__(
        self,
        *,
        revamp_regime_id: int | None = None,
        revamp_special_id: int | None = None,
        revamp_name: str | None = None,
        existing: Suggestion | None = None,
        parent_view: "SuggestionsView | None" = None,
    ):
        revamp_regime_id = revamp_regime_id or (existing.revamp_regime_id if existing else None)
        revamp_special_id = revamp_special_id or (existing.revamp_special_id if existing else None)
        is_revamp = revamp_regime_id is not None or revamp_special_id is not None

        if existing is not None:
            super().__init__(title="Edit Card Suggestion")
        elif is_revamp:
            super().__init__(title="Revamp Card Suggestion")
        else:
            super().__init__()
        self.draft = SuggestionDraft(
            edit_pk=existing.pk if existing else None,
            type=Suggestion.Type.CARD,
            card_type=(
                existing.card_type
                if existing
                else Suggestion.CardType.REGIME
                if revamp_regime_id is not None
                else Suggestion.CardType.SPECIAL
                if revamp_special_id is not None
                else None
            ),
            is_revamp=is_revamp,
            revamp_regime_id=revamp_regime_id,
            revamp_special_id=revamp_special_id,
        )
        self.existing = existing
        self.parent_view = parent_view
        self.is_revamp = is_revamp
        self._blank_name_fallback = existing.name if existing is not None else revamp_name

        self.name_input = TextInput(
            placeholder="The name of this card",
            max_length=64,
            required=not is_revamp,
            default=existing.name if existing is not None else (revamp_name if is_revamp else None),
        )
        self.add_item(
            Label(
                text="Name",
                description="Optional. Leave blank to keep the current name." if is_revamp else None,
                component=self.name_input,
            )
        )

        self.card_type_select: Select | None = None
        if not is_revamp:
            self.card_type_select = Select(
                options=[
                    SelectOption(
                        label="Regime",
                        value=Suggestion.CardType.REGIME,
                        default=existing is not None and existing.card_type == Suggestion.CardType.REGIME,
                    ),
                    SelectOption(
                        label="Special",
                        value=Suggestion.CardType.SPECIAL,
                        default=existing is not None and existing.card_type == Suggestion.CardType.SPECIAL,
                    ),
                ]
            )
            self.add_item(Label(text="Card Type", component=self.card_type_select))

        background_required = existing is None and not is_revamp
        self.background_upload = FileUpload(max_values=1, required=background_required)
        self.add_item(
            Label(
                text="Background Art",
                description=(
                    "1428x2000 PNG background art."
                    if background_required
                    else "Optional. Leave blank to keep the current image."
                ),
                component=self.background_upload,
            )
        )

    async def on_submit(self, interaction: Interaction):
        files = self.background_upload.values
        if self.existing is None and not self.is_revamp and not files:
            await interaction.response.send_message("Background art is required.", ephemeral=True)
            return
        if files and (error := _validate_image(files[0])):
            await interaction.response.send_message(error, ephemeral=True)
            return

        if self.card_type_select is not None:
            if not self.card_type_select.values:
                await interaction.response.send_message("You must pick a card type.", ephemeral=True)
                return
            card_type = self.card_type_select.values[0]
        else:
            card_type = self.draft.card_type

        name = self.name_input.value.strip()
        if not name:
            if not self.is_revamp or not self._blank_name_fallback:
                await interaction.response.send_message("You must set a name.", ephemeral=True)
                return
            name = self._blank_name_fallback

        await interaction.response.defer(ephemeral=True, thinking=True)

        self.draft.name = name
        self.draft.card_type = card_type
        if files:
            self.draft.card_background_filename = files[0].filename
            self.draft.card_background_bytes = await files[0].read()

        if card_type == Suggestion.CardType.SPECIAL:
            draft = self.draft
            existing = self.existing
            parent_view = self.parent_view
            view = ContinueView(
                label="Continue (2/3)",
                next_modal_factory=lambda: CardSpecialDetailsModal(draft, existing=existing, parent_view=parent_view),
            )
            await interaction.followup.send(
                "Step 1/3 saved. Click below to continue with the Special event's details.",
                view=view,
                ephemeral=True,
            )
            return

        await _finalize_suggestion(interaction, self.draft, self.existing, self.parent_view)


class CardSpecialDetailsModal(Modal, title="New Card Suggestion — Step 2/3"):
    def __init__(
        self,
        draft: SuggestionDraft,
        *,
        existing: Suggestion | None = None,
        parent_view: "SuggestionsView | None" = None,
    ):
        if existing is not None:
            super().__init__(title="Edit Card Suggestion — Step 2/3")
        else:
            super().__init__()
        self.draft = draft
        self.existing = existing
        self.parent_view = parent_view

        self.catch_phrase_input = TextInput(
            placeholder="Sentence sent when someone catches a special card",
            max_length=128,
            required=False,
            default=existing.catch_phrase if existing is not None else None,
        )
        self.add_item(
            Label(
                text="Catch Phrase",
                description="Optional. Sent when someone catches a card of this event.",
                component=self.catch_phrase_input,
            )
        )

        self.emoji_input = TextInput(
            placeholder="A unicode character",
            max_length=16,
            required=not draft.is_revamp,
            default=existing.emoji if existing is not None else None,
        )
        self.add_item(
            Label(
                text="Emoji",
                description="Type the Unicode emoji itself (e.g. \N{JACK-O-LANTERN}), not its ID.",
                component=self.emoji_input,
            )
        )

        self.credits_input = TextInput(
            placeholder="Author of the special event artwork",
            max_length=64,
            required=not draft.is_revamp,
            default=existing.credits if existing is not None else None,
        )
        self.add_item(Label(text="Credits", description="Artwork credits.", component=self.credits_input))

    async def on_submit(self, interaction: Interaction):
        emoji = self.emoji_input.value.strip()
        if not emoji and not self.draft.is_revamp:
            await interaction.response.send_message("Emoji is required.", ephemeral=True)
            return

        credits = self.credits_input.value.strip()
        if not credits and not self.draft.is_revamp:
            await interaction.response.send_message("Credits are required.", ephemeral=True)
            return

        self.draft.catch_phrase = self.catch_phrase_input.value.strip() or None
        if emoji:
            self.draft.emoji = emoji
        if credits:
            self.draft.credits = credits

        draft = self.draft
        existing = self.existing
        parent_view = self.parent_view
        view = ContinueView(
            label="Continue (3/3)",
            next_modal_factory=lambda: CardSpecialScheduleModal(draft, existing=existing, parent_view=parent_view),
        )
        await interaction.response.send_message(
            "Step 2/3 saved. Click below to finish with the event's rarity and schedule.",
            view=view,
            ephemeral=True,
        )


class CardSpecialScheduleModal(Modal, title="New Card Suggestion — Step 3/3"):
    def __init__(
        self,
        draft: SuggestionDraft,
        *,
        existing: Suggestion | None = None,
        parent_view: "SuggestionsView | None" = None,
    ):
        if existing is not None:
            super().__init__(title="Edit Card Suggestion — Step 3/3")
        else:
            super().__init__()
        self.draft = draft
        self.existing = existing
        self.parent_view = parent_view

        self.rarity_input = TextInput(
            placeholder="A value between 0 and 1, chances of using this special card.",
            required=not draft.is_revamp,
            default=str(existing.rarity) if existing is not None and existing.rarity is not None else None,
        )
        self.add_item(
            Label(
                text="Rarity",
                description="Chances of this event's background being used.",
                component=self.rarity_input,
            )
        )

        self.start_date_input = TextInput(
            placeholder="e.g. 2026-12-01 00:00",
            required=False,
            default=(
                existing.start_date.strftime("%Y-%m-%d %H:%M")
                if existing is not None and existing.start_date
                else None
            ),
        )
        self.add_item(
            Label(
                text="Start Date",
                description="Optional, format YYYY-MM-DD HH:MM (UTC). Blank starts immediately.",
                component=self.start_date_input,
            )
        )

        self.end_date_input = TextInput(
            placeholder="e.g. 2026-12-31 23:59",
            required=False,
            default=(
                existing.end_date.strftime("%Y-%m-%d %H:%M") if existing is not None and existing.end_date else None
            ),
        )
        self.add_item(
            Label(
                text="End Date",
                description="Optional, format YYYY-MM-DD HH:MM (UTC). Blank runs permanently.",
                component=self.end_date_input,
            )
        )

    async def on_submit(self, interaction: Interaction):
        rarity_value = self.rarity_input.value.strip()
        if rarity_value:
            try:
                rarity = float(rarity_value)
            except ValueError:
                await interaction.response.send_message("Rarity must be a number, e.g. `0.05`.", ephemeral=True)
                return
        elif self.draft.is_revamp:
            rarity = None
        else:
            await interaction.response.send_message("Rarity must be a number, e.g. `0.05`.", ephemeral=True)
            return

        start_date = None
        if self.start_date_input.value.strip():
            start_date = _parse_datetime(self.start_date_input.value)
            if start_date is None:
                await interaction.response.send_message(
                    "Start Date must look like `YYYY-MM-DD HH:MM`.", ephemeral=True
                )
                return

        end_date = None
        if self.end_date_input.value.strip():
            end_date = _parse_datetime(self.end_date_input.value)
            if end_date is None:
                await interaction.response.send_message(
                    "End Date must look like `YYYY-MM-DD HH:MM`.", ephemeral=True
                )
                return

        await interaction.response.defer(ephemeral=True, thinking=True)

        self.draft.rarity = rarity
        self.draft.start_date = start_date
        self.draft.end_date = end_date

        await _finalize_suggestion(interaction, self.draft, self.existing, self.parent_view)




class EconomyModal(Modal, title="New Economy Suggestion"):
    def __init__(
        self,
        *,
        revamp_economy_id: int | None = None,
        revamp_economy_name: str | None = None,
        existing: Suggestion | None = None,
        parent_view: "SuggestionsView | None" = None,
    ):
        revamp_economy_id = revamp_economy_id or (existing.revamp_economy_id if existing else None)
        is_revamp = revamp_economy_id is not None
        if existing is not None:
            super().__init__(title="Edit Economy Suggestion")
        elif is_revamp:
            super().__init__(title="Revamp Economy Suggestion")
        else:
            super().__init__()
        self.draft = SuggestionDraft(
            edit_pk=existing.pk if existing else None,
            type=Suggestion.Type.ECONOMY,
            is_revamp=is_revamp,
            revamp_economy_id=revamp_economy_id,
        )
        self.existing = existing
        self.parent_view = parent_view
        self._blank_name_fallback = existing.name if existing is not None else revamp_economy_name

        self.name_input = TextInput(
            placeholder="Name of this economy",
            max_length=64,
            required=not is_revamp,
            default=existing.name if existing is not None else (revamp_economy_name if is_revamp else None),
        )
        self.add_item(
            Label(
                text="Name",
                description="Optional. Leave blank to keep the current name." if is_revamp else None,
                component=self.name_input,
            )
        )

        icon_required = existing is None and revamp_economy_id is None
        self.icon_upload = FileUpload(max_values=1, required=icon_required)
        self.add_item(
            Label(
                text="Icon",
                description=(
                    "512x512 PNG icon image."
                    if icon_required
                    else "Optional. Leave blank to keep the current image."
                ),
                component=self.icon_upload,
            )
        )

    async def on_submit(self, interaction: Interaction):
        files = self.icon_upload.values
        if self.existing is None and not self.draft.is_revamp and not files:
            await interaction.response.send_message("An icon image is required.", ephemeral=True)
            return
        if files and (error := _validate_image(files[0])):
            await interaction.response.send_message(error, ephemeral=True)
            return

        name = self.name_input.value.strip()
        if not name:
            if not self.draft.is_revamp or not self._blank_name_fallback:
                await interaction.response.send_message("You must set a name.", ephemeral=True)
                return
            name = self._blank_name_fallback

        await interaction.response.defer(ephemeral=True, thinking=True)

        self.draft.name = name
        if files:
            self.draft.economy_icon_filename = files[0].filename
            self.draft.economy_icon_bytes = await files[0].read()

        await _finalize_suggestion(interaction, self.draft, self.existing, self.parent_view)


class BallRevampSearchModal(Modal, title="Revamp a Countryball"):
    """
    No fuzzy search, no picker - the suggester just types the exact real Ball name and this jumps
    straight into the normal creation flow (`BasicInfoModal`) with `revamp_ball_id` already set.
    The class-level `title=` above is just a fallback default the metaclass requires; `__init__`
    always overrides it with the server's actual collectible name before anyone sees it.
    """

    def __init__(self, parent_view: "SuggestionsView | None" = None):
        super().__init__(title=f"Revamp a {settings.collectible_name.title()}")
        self.parent_view = parent_view
        self.name_input = _add_exact_name_field(
            self,
            label=f"{settings.collectible_name.title()} Name",
            description=f"Must match an existing {settings.collectible_name}'s name exactly.",
        )

    async def on_submit(self, interaction: Interaction):
        name = self.name_input.value.strip()
        ball = await SuggestionsService.get_ball_by_exact_name(name)
        if ball is None:
            await interaction.response.send_message(
                f"No {settings.collectible_name} found named exactly `{name}`. Check the spelling and try again.",
                ephemeral=True,
            )
            return
        regimes, economies, missing_reason = await _load_countryball_requirements()
        if missing_reason is not None:
            await interaction.response.send_message(missing_reason, ephemeral=True)
            return
        view = ContinueView(
            label="Continue",
            next_modal_factory=lambda: BasicInfoModal(
                regimes,
                economies,
                revamp_ball_id=ball.pk,
                revamp_ball_name=ball.country,
                parent_view=self.parent_view,
            ),
        )
        await interaction.response.send_message(
            f"Found `{name}`. Click below to continue.", view=view, ephemeral=True
        )


class CardRevampSearchModal(Modal, title="Revamp a Card"):
    """
    Same "type the exact name" flow as `BallRevampSearchModal`, plus a Card Type select - same
    Regime/Special choice `CardBasicModal` asks for on a fresh Card suggestion. Asking upfront
    (rather than searching Regime and Special together) means the name lookup only ever touches
    the one table the suggester actually meant, so a Regime and a Special sharing the same name
    can never be ambiguous here the way it could when both were checked at once.
    """

    def __init__(self, parent_view: "SuggestionsView | None" = None):
        super().__init__()
        self.parent_view = parent_view
        self.name_input = _add_exact_name_field(
            self,
            label="Card Name",
            description="Must match an existing Regime or Special event's name exactly.",
        )

        self.card_type_select = Select(
            options=[
                SelectOption(label="Regime", value=Suggestion.CardType.REGIME),
                SelectOption(label="Special", value=Suggestion.CardType.SPECIAL),
            ]
        )
        self.add_item(Label(text="Card Type", component=self.card_type_select))

    async def on_submit(self, interaction: Interaction):
        if not self.card_type_select.values:
            await interaction.response.send_message("You must pick a card type.", ephemeral=True)
            return
        card_type = self.card_type_select.values[0]

        name = self.name_input.value.strip()
        if card_type == Suggestion.CardType.REGIME:
            regime = await SuggestionsService.get_regime_by_exact_name(name)
            if regime is None:
                await interaction.response.send_message(
                    f"No Regime found named exactly `{name}`. Check the spelling and try again.", ephemeral=True
                )
                return
            view = ContinueView(
                label="Continue",
                next_modal_factory=lambda: CardBasicModal(
                    revamp_regime_id=regime.pk, revamp_name=regime.name, parent_view=self.parent_view
                ),
            )
            await interaction.response.send_message(
                f"Found `{name}`. Click below to continue.", view=view, ephemeral=True
            )
        else:
            special = await SuggestionsService.get_special_by_exact_name(name)
            if special is None:
                await interaction.response.send_message(
                    f"No Special event found named exactly `{name}`. Check the spelling and try again.",
                    ephemeral=True,
                )
                return
            view = ContinueView(
                label="Continue",
                next_modal_factory=lambda: CardBasicModal(
                    revamp_special_id=special.pk, revamp_name=special.name, parent_view=self.parent_view
                ),
            )
            await interaction.response.send_message(
                f"Found `{name}`. Click below to continue.", view=view, ephemeral=True
            )


class EconomyRevampSearchModal(Modal, title="Revamp an Economy"):
    """Same "type the exact name" flow as `BallRevampSearchModal`."""

    def __init__(self, parent_view: "SuggestionsView | None" = None):
        super().__init__()
        self.parent_view = parent_view
        self.name_input = _add_exact_name_field(
            self,
            label="Economy Name",
            description="Must match an existing Economy's name exactly.",
        )

    async def on_submit(self, interaction: Interaction):
        name = self.name_input.value.strip()
        economy = await SuggestionsService.get_economy_by_exact_name(name)
        if economy is None:
            await interaction.response.send_message(
                f"No Economy found named exactly `{name}`. Check the spelling and try again.",
                ephemeral=True,
            )
            return
        view = ContinueView(
            label="Continue",
            next_modal_factory=lambda: EconomyModal(
                revamp_economy_id=economy.pk, revamp_economy_name=economy.name, parent_view=self.parent_view
            ),
        )
        await interaction.response.send_message(
            f"Found `{name}`. Click below to continue.", view=view, ephemeral=True
        )




class RequestChangesModal(Modal, title="Request Changes"):
    comment = Label(
        text="What needs to change?",
        component=TextInput(style=discord.TextStyle.paragraph, max_length=256),
    )

    def __init__(self, parent_view: "SuggestionsView"):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: Interaction):
        assert self.parent_view.selected is not None
        suggestion = self.parent_view.selected
        comment = self.comment.component.value.strip()

        await SuggestionsService.set_status(suggestion.pk, Suggestion.Status.CHANGES_REQUESTED, comment)
        self.parent_view.viewing_user = interaction.user
        await self.parent_view._reload_data(keep_selection=True)
        await interaction.response.edit_message(
            view=self.parent_view, attachments=self.parent_view.current_files
        )

        dm_content = (
            f"**Action Requested on your {settings.bot_name} Suggestion!**\n"
            f"An admin reviewed your suggestion for **{suggestion.name}** and requested changes:\n"
            f"{_quote_block(comment)}\n\n"
            "Use `/suggestions` to select and edit your submission!"
        )
        try:
            user = self.parent_view.bot.get_user(
                suggestion.submitted_by.discord_id
            ) or await self.parent_view.bot.fetch_user(suggestion.submitted_by.discord_id)
            await user.send(dm_content)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "Status updated, but I couldn't DM the suggester (they may have DMs disabled).",
                ephemeral=True,
            )


class AddCommentModal(Modal, title="Add Comment"):
    comment = Label(
        text="Your Comment",
        component=TextInput(style=discord.TextStyle.paragraph, max_length=MAX_COMMENT_LENGTH),
    )

    def __init__(self, parent_view: "CommentsView"):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: Interaction):
        try:
            status = await SuggestionsService.get_status(self.parent_view.suggestion_id)
        except Suggestion.DoesNotExist:
            await interaction.response.send_message("This suggestion no longer exists.", ephemeral=True)
            return
        if status == Suggestion.Status.APPROVED:
            await interaction.response.send_message(
                "This suggestion has already been approved - comments are locked.", ephemeral=True
            )
            return

        content = self.comment.component.value.strip()
        await SuggestionsService.add_comment(self.parent_view.suggestion_id, interaction.user.id, content)
        log.info(
            f"{interaction.user} commented on suggestion #{self.parent_view.suggestion_id}",
            extra={"webhook": True},
        )
        await self.parent_view._reload_data(go_to_last_page=True)
        await interaction.response.edit_message(view=self.parent_view)


class CommentFormatter(Formatter["QuerySet[SuggestionComment]", TextDisplay]):
    """
    Renders one page of a suggestion's comment thread (a `SuggestionComment` queryset slice, from
    `ModelSource`) into a single `TextDisplay` - the ballsdex menu system's bridge between a page
    of DB rows and one of our own UI elements, matching `CountryballFormatter`'s role for balls.
    """

    async def format_page(self, page: "QuerySet[SuggestionComment]"):
        lines = []
        async for comment in page:
            timestamp = discord.utils.format_dt(comment.created_at, style="R")
            lines.append(f"**<@{comment.author.discord_id}>** • {timestamp}\n{comment.content}")
        self.item.content = "\n\n".join(lines) if lines else "*No comments yet - be the first to ask something!*"


class CommentsView(LayoutView):
    """
    A private, paginated view of the full comment thread on one suggestion - lets community
    members (and the suggester) hash out accuracy questions without that dialogue cluttering the
    shared browsing card (see `SuggestionsView._on_comments` below, which opens this ephemerally).

    Since this is only ever sent ephemeral - one fresh copy per person who clicks "Comments" - it
    doesn't need `SuggestionsView`'s "last interactor" trick: the person who opened it is the only
    one who can ever see or act on it, so `interaction.user` is always the real, current viewer.

    Pagination data comes from the `ballsdex.core.utils.menus` `Menu`/`ModelSource` (feeding the
    `CommentFormatter` above) instead of hand-rolled page math, but the nav *buttons* are our own
    compact Back/Next pair sharing a row with "Add Comment" - the framework's stock `Controls` is a
    5-button row (≪/Back/go-to/Next/≫) that doesn't leave room for another button alongside it, so
    we only take the menu system's state tracking here, not its UI.
    """

    def __init__(
        self, bot: "BallsDexBot", suggestion_id: int, suggestion_name: str, *, timeout: float | None = None
    ):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.suggestion_id = suggestion_id
        self.suggestion_name = suggestion_name
        self.comments_display = TextDisplay("")
        self.suggestion_locked = False

        self._previous_button = Button(
            emoji="\N{BLACK LEFT-POINTING TRIANGLE}", style=ButtonStyle.grey, custom_id="comments_previous"
        )
        self._previous_button.callback = self._on_previous
        self._next_button = Button(
            emoji="\N{BLACK RIGHT-POINTING TRIANGLE}", style=ButtonStyle.grey, custom_id="comments_next"
        )
        self._next_button.callback = self._on_next
        self._add_button = Button(
            label="Add Comment", emoji="\N{MEMO}", style=ButtonStyle.blurple, custom_id="comments_add"
        )
        self._add_button.callback = self._on_add_comment

        source = ModelSourceAllowEmpty(
            SuggestionsService.get_comments_queryset(suggestion_id), per_page=COMMENTS_PER_PAGE
        )
        self.menu = Menu(bot, self, source, CommentFormatter(self.comments_display))

    async def initialize(self):
        await self._reload_data()

    async def _reload_data(self, *, go_to_last_page: bool = False, page: int | None = None):
        try:
            status = await SuggestionsService.get_status(self.suggestion_id)
            self.suggestion_locked = status == Suggestion.Status.APPROVED
        except Suggestion.DoesNotExist:
            self.suggestion_locked = True

        await self.menu.source.prepare()
        max_page = self.menu.source.get_max_pages() - 1
        if go_to_last_page:
            target_page = max_page
        elif page is not None:
            target_page = max(0, min(page, max_page))
        else:
            target_page = min(self.menu.current_page, max_page)
        await self.menu.set_page(target_page)
        self._render()

    def _render(self):
        self.clear_items()
        container = Container()

        container.add_item(TextDisplay(f"💬 **Comments — {self.suggestion_name}**"))
        container.add_item(Separator())
        container.add_item(self.comments_display)
        container.add_item(Separator())

        nav_row = ActionRow()
        if self.menu.source.get_max_pages() > 1:
            self._previous_button.disabled = self.menu.current_page <= 0
            self._next_button.disabled = self.menu.current_page >= self.menu.source.get_max_pages() - 1
            nav_row.add_item(self._previous_button)
            nav_row.add_item(self._next_button)
        else:
            _forget_dispatch(self, self._previous_button)
            _forget_dispatch(self, self._next_button)

        if not self.suggestion_locked:
            nav_row.add_item(self._add_button)
        else:
            _forget_dispatch(self, self._add_button)

        if nav_row.children:
            container.add_item(nav_row)

        self.add_item(container)

    async def _on_previous(self, interaction: Interaction):
        await interaction.response.defer()
        await self._reload_data(page=self.menu.current_page - 1)
        await interaction.edit_original_response(view=self)

    async def _on_next(self, interaction: Interaction):
        await interaction.response.defer()
        await self._reload_data(page=self.menu.current_page + 1)
        await interaction.edit_original_response(view=self)

    async def _on_add_comment(self, interaction: Interaction):
        await interaction.response.send_modal(AddCommentModal(self))


class SuggestionFormatter(Formatter["QuerySet[Suggestion]", Select]):
    """
    Renders one page of the suggestion list (a `Suggestion` queryset slice, from `ModelSource`)
    into the browsing `Select`'s options - the ballsdex menu system's bridge between a page of DB
    rows and one of our own UI elements, matching `CountryballFormatter`'s role for balls. Also
    stashes the materialized page on the view as `items`, since the plain-text summary list above
    the select (see `SuggestionsView._render`) needs the same suggestions and a queryset page can
    only be iterated once.
    """

    async def format_page(self, page: "QuerySet[Suggestion]"):
        view: "SuggestionsView" = self.menu.view
        view.items = [suggestion async for suggestion in page]
        if view.items:
            self.item.options = [
                SelectOption(
                    label=suggestion.name[:100],
                    description=view._select_subtitle(suggestion)[:100],
                    value=str(suggestion.pk),
                    default=view.selected is not None and suggestion.pk == view.selected.pk,
                )
                for suggestion in view.items
            ]
            self.item.disabled = False
        else:
            self.item.options = [SelectOption(label="No suggestions available", value=NO_SUGGESTION_VALUE)]
            self.item.disabled = True


class SuggestionsMenu(Menu["QuerySet[Suggestion]"]):
    """
    We use our own compact Back/Next buttons (see `SuggestionsView._render`) rather than the
    framework's stock `Controls` - a 5-button row (≪/Back/go-to/Next/≫) that doesn't leave room to
    also fit "New Suggestion" in the same row - but they still call into this menu's `show_page`,
    same as `Controls`' own buttons would. The base `Menu.show_page()` just calls
    `interaction.edit_original_response(view=self.view)`, which has no idea our view also carries
    file attachments (the selected suggestion's art) that must travel with every edit, or that
    turning the page makes the current selection stale. This override handles both by just
    delegating to the view's own `_reload_data`, which already does.
    """

    async def show_page(self, interaction: Interaction, page: int):
        await interaction.response.defer()
        self.view.viewing_user = interaction.user
        await self.view._reload_data(keep_selection=False, page=page)
        await interaction.edit_original_response(view=self.view, attachments=self.view.current_files)


class SuggestionsView(LayoutView):
    """
    The "Community Suggestions" browsing menu, scoped to a single `type` (Countryball, Card, or
    Economy - picked via the `/suggestions` command's `type` parameter, see `cog.py`).

    Everything lives in a single, always-rebuilt :class:`Container`: the page of suggestions, the
    currently selected suggestion's details (if any), a row of vote/edit/moderation actions, page
    navigation, and the select menu used to pick a suggestion. Selecting an entry, voting, or
    moderating all just mutate this view's state and re-render the same message in place.

    Only the person who ran `/suggestions` can interact with their own copy of this menu at all
    (`restrict_author` in `__init__`) - everyone else who wants to browse/vote/moderate just runs
    the command themselves to get their own message. `viewing_user` is therefore always that same
    person; it's kept as a distinct field (rather than reusing the constructor's `invoker`) purely
    so every render reads it the same way regardless of which callback last ran. The moderation/edit
    callbacks still re-check the actual clicker's permissions independently, so that's purely a
    display choice, never the security boundary.
    """

    def __init__(
        self,
        bot: "BallsDexBot",
        invoker: "discord.abc.User",
        *,
        type: str = Suggestion.Type.COUNTRYBALL,
        is_revamp: bool = False,
        sort: str = "creation_date",
        timeout: float | None = None,
    ):
        super().__init__(timeout=timeout)
        self.restrict_author(invoker.id)
        self.bot = bot
        self.type = type
        self.is_revamp = is_revamp
        self.sort = sort
        self.viewing_user: "discord.abc.User" = invoker
        self.items: list[Suggestion] = []
        self.selected: Suggestion | None = None
        self.viewer_is_submitter = False
        self.viewer_has_voted = False
        self.viewer_can_moderate = False
        self.viewer_can_edit = False
        self.new_suggestion_blocked_reason: str | None = None
        self._select_item = Select(placeholder="Inspect a suggestion.")
        self._select_item.callback = self._on_select
        source = ModelSourceAllowEmpty(
            SuggestionsService.get_queryset(type, is_revamp=is_revamp, sort=sort), per_page=PER_PAGE
        )
        self.menu = SuggestionsMenu(bot, self, source, SuggestionFormatter(self._select_item))
        self.current_files: list[discord.File] = []
        self.current_gallery_items: list[MediaGalleryItem] = []

        self._vote_button = Button(style=ButtonStyle.blurple, custom_id="suggestions_vote")
        self._vote_button.callback = self._on_upvote
        self._comments_button = Button(emoji="💬", style=ButtonStyle.grey, custom_id="suggestions_comments")
        self._comments_button.callback = self._on_comments
        self._edit_button = Button(
            label="Edit Submission", emoji="✍️", style=ButtonStyle.grey, custom_id="suggestions_edit"
        )
        self._edit_button.callback = self._on_edit_submission

        self._approve_button = Button(label="Approve", style=ButtonStyle.green, custom_id="suggestions_approve")
        self._approve_button.callback = self._on_approve
        self._deny_button = Button(label="Deny", style=ButtonStyle.red, custom_id="suggestions_deny")
        self._deny_button.callback = self._on_deny
        self._changes_button = Button(
            label="Request Changes", style=ButtonStyle.grey, custom_id="suggestions_request_changes"
        )
        self._changes_button.callback = self._on_request_changes

        self._previous_button = Button(
            emoji="\N{BLACK LEFT-POINTING TRIANGLE}", style=ButtonStyle.grey, custom_id="suggestions_previous"
        )
        self._previous_button.callback = self._on_previous
        self._next_button = Button(
            emoji="\N{BLACK RIGHT-POINTING TRIANGLE}", style=ButtonStyle.grey, custom_id="suggestions_next"
        )
        self._next_button.callback = self._on_next
        self._new_button = Button(emoji="\N{HEAVY PLUS SIGN}", style=ButtonStyle.success, custom_id="suggestions_new")
        self._new_button.callback = self._on_new_suggestion

    async def initialize(self):
        await self._reload_data(keep_selection=False)

    async def _recompute_viewer_state(self):
        self.viewer_can_moderate = await SuggestionsService.is_staff(self.bot, self.viewing_user)
        if self.selected is not None:
            self.viewer_is_submitter = self.selected.submitted_by.discord_id == self.viewing_user.id
            self.viewer_has_voted = await SuggestionsService.has_voted(self.selected.pk, self.viewing_user.id)
        else:
            self.viewer_is_submitter = False
            self.viewer_has_voted = False
        self.viewer_can_edit = self.viewer_is_submitter or self.viewer_can_moderate

    async def _load_art(self) -> None:
        """
        Refreshes `current_files`/`current_gallery_items` for the currently selected suggestion.
        Attaching the art directly as Discord files - instead of linking to wherever the bot's web
        server happens to serve `MEDIA_URL` - means it renders regardless of whether that's even
        publicly reachable. Must be awaited (after `self.selected` is set) before `_render()`, and
        `current_files` must accompany the very next send/edit alongside `view=self` - `files=` on
        an initial `send_message`, `attachments=` on every edit - or the gallery items just show as
        broken images.
        """
        if self.selected is None:
            self.current_files = []
            self.current_gallery_items = []
            return

        art = await SuggestionsService.get_art_files(self.selected)
        self.current_files = [file for file, _ in art]
        self.current_gallery_items = [
            MediaGalleryItem(f"attachment://{file.filename}", description=description) for file, description in art
        ]

    async def _reload_data(self, *, keep_selection: bool, page: int | None = None):
        if keep_selection and self.selected is not None:
            try:
                self.selected = await SuggestionsService.get_detail(self.selected.pk)
            except Suggestion.DoesNotExist:
                self.selected = None
        elif not keep_selection:
            self.selected = None

        await self.menu.source.prepare()
        target_page = self.menu.current_page if page is None else page
        target_page = max(0, min(target_page, self.menu.source.get_max_pages() - 1))
        await self.menu.set_page(target_page)

        await self._load_art()
        await self._recompute_viewer_state()
        if self.type == Suggestion.Type.COUNTRYBALL:
            _, _, self.new_suggestion_blocked_reason = await _load_countryball_requirements()
        else:
            self.new_suggestion_blocked_reason = None
        self._render()

    def _render(self):
        self.clear_items()
        container = Container()
        if self.selected is not None:
            container.accent_colour = STATUS_ACCENT_COLOURS.get(self.selected.status)

        label = f"{_type_label(self.type)} Revamps" if self.is_revamp else _type_label(self.type)
        header = f"🌐 **Community Suggestions — {label}**"
        if self.sort == "upvotes":
            header += "\n-# Sorted by upvotes"
        container.add_item(TextDisplay(header))
        container.add_item(Separator())

        if self.items:
            lines = []
            for suggestion in self.items:
                votes = getattr(suggestion, "vote_count", 0)
                lines.append(f"- **{suggestion.name}** — ⬆️ {votes} vote{'s' if votes != 1 else ''}")
            container.add_item(TextDisplay("\n".join(lines)))
        else:
            placeholder = (
                "*No revamps suggested yet, be the first to submit one!*"
                if self.is_revamp
                else "*No suggestions yet, be the first to submit one!*"
            )
            container.add_item(TextDisplay(placeholder))

        not_yet_approved = self.selected is not None and self.selected.status != Suggestion.Status.APPROVED

        if self.selected is not None:
            container.add_item(Separator())
            container.add_item(TextDisplay(self._build_selected_text()))

            if self.current_gallery_items:
                container.add_item(MediaGallery(*self.current_gallery_items))

            if self.viewer_has_voted:
                self._vote_button.label = "Remove Upvote"
                self._vote_button.emoji = None
                self._vote_button.style = ButtonStyle.red
            else:
                self._vote_button.label = "Upvote"
                self._vote_button.emoji = "⬆️"
                self._vote_button.style = ButtonStyle.blurple

            comment_count = getattr(self.selected, "comment_count", 0)
            self._comments_button.label = f"Comments ({comment_count})"

            action_row = ActionRow()
            action_row.add_item(self._vote_button)
            action_row.add_item(self._comments_button)

            if not_yet_approved and self.viewer_can_edit:
                action_row.add_item(self._edit_button)
            else:
                _forget_dispatch(self, self._edit_button)

            container.add_item(action_row)

            if not_yet_approved and self.viewer_can_moderate:
                mod_row = ActionRow()
                mod_row.add_item(self._approve_button)
                mod_row.add_item(self._deny_button)
                mod_row.add_item(self._changes_button)
                container.add_item(mod_row)
            else:
                _forget_dispatch(self, self._approve_button)
                _forget_dispatch(self, self._deny_button)
                _forget_dispatch(self, self._changes_button)
        else:
            _forget_dispatch(self, self._vote_button)
            _forget_dispatch(self, self._comments_button)
            _forget_dispatch(self, self._edit_button)
            _forget_dispatch(self, self._approve_button)
            _forget_dispatch(self, self._deny_button)
            _forget_dispatch(self, self._changes_button)

        container.add_item(Separator())

        nav_row = ActionRow()
        if self.menu.source.get_max_pages() > 1:
            self._previous_button.disabled = self.menu.current_page <= 0
            self._next_button.disabled = self.menu.current_page >= self.menu.source.get_max_pages() - 1
            nav_row.add_item(self._previous_button)
            nav_row.add_item(self._next_button)
        else:
            _forget_dispatch(self, self._previous_button)
            _forget_dispatch(self, self._next_button)

        self._new_button.label = "New Revamp" if self.is_revamp else "New Suggestion"
        self._new_button.disabled = self.new_suggestion_blocked_reason is not None
        nav_row.add_item(self._new_button)
        container.add_item(nav_row)

        if self.items:
            select_row = ActionRow()
            select_row.add_item(self._select_item)
            container.add_item(select_row)
        else:
            _forget_dispatch(self, self._select_item)

        self.add_item(container)

    def _select_subtitle(self, suggestion: Suggestion) -> str:
        votes = getattr(suggestion, "vote_count", 0)
        if suggestion.type == Suggestion.Type.COUNTRYBALL:
            subtitle = suggestion.regime.name if suggestion.regime_id else "Unknown regime"
        elif suggestion.type == Suggestion.Type.CARD:
            subtitle = suggestion.get_card_type_display() if suggestion.card_type else "Card"
        else:
            subtitle = "Economy"
        return f"{subtitle} • {votes} votes"

    def _build_selected_text(self) -> str:
        assert self.selected is not None
        s = self.selected
        status = s.get_status_display()
        lines = [
            f"**Selected: {s.name}**",
            f"-# Suggester: <@{s.submitted_by.discord_id}> • Status: {status}",
        ]

        target = None
        target_name = None
        if s.is_revamp:
            if s.type == Suggestion.Type.COUNTRYBALL:
                target = s.revamp_ball if s.revamp_ball_id else None
                target_name = target.country if target else None
            elif s.card_type == Suggestion.CardType.REGIME:
                target = s.revamp_regime if s.revamp_regime_id else None
                target_name = target.name if target else None
            elif s.type == Suggestion.Type.CARD:
                target = s.revamp_special if s.revamp_special_id else None
                target_name = target.name if target else None
            else:
                target = s.revamp_economy if s.revamp_economy_id else None
                target_name = target.name if target else None
            if target_name is None:
                lines.append("* **Note:** the target of this revamp has been deleted since it was submitted.")

        diffing_active = target is not None and s.status != Suggestion.Status.APPROVED

        def keep(field: str, *, always_show_blank: bool = False) -> bool:
            suggested = getattr(s, field)
            if diffing_active:
                if suggested is None:
                    return False
                return suggested != getattr(target, field, None)
            return suggested is not None or always_show_blank

        if not diffing_active or s.name != target_name:
            lines.append(f"* **Name:** {s.name}")

        if s.type == Suggestion.Type.COUNTRYBALL:
            if s.group_id:
                current_group_ids = {g.pk for g in target.groups.all()} if diffing_active else set()
                if not diffing_active or s.group_id not in current_group_ids:
                    lines.append(f"* **Group:** {s.group.name}")

            countryball_fields: list[tuple[str, str, Callable[[], object]]] = [
                ("short_name", "Short Name", lambda: s.short_name),
                ("catch_names", "Catch Names", lambda: s.catch_names),
                ("regime_id", "Regime", lambda: s.regime.name if s.regime_id else "N/A"),
                ("economy_id", "Economy", lambda: s.economy.name),
                ("health", "Health", lambda: s.health),
                ("attack", "Attack", lambda: s.attack),
                ("rarity", "Rarity", lambda: s.rarity),
                ("capacity_name", "Ability", lambda: s.capacity_name),
                ("capacity_description", "Ability Description", lambda: s.capacity_description),
                ("credits", "Artwork Author", lambda: s.credits or "N/A"),
            ]
            for field, label, display in countryball_fields:
                if keep(field):
                    lines.append(f"* **{label}:** {display()}")

        elif s.type == Suggestion.Type.CARD:
            if not s.is_revamp:
                lines.append(f"* **Card Type:** {s.get_card_type_display() if s.card_type else 'N/A'}")

            if s.card_type == Suggestion.CardType.SPECIAL:
                special_fields: list[tuple[str, str, Callable[[], object]]] = [
                    ("emoji", "Emoji", lambda: s.emoji or "N/A"),
                    ("catch_phrase", "Catch Phrase", lambda: s.catch_phrase),
                    ("rarity", "Rarity", lambda: s.rarity),
                    ("credits", "Artwork Author", lambda: s.credits or "N/A"),
                    (
                        "start_date",
                        "Start Date",
                        lambda: discord.utils.format_dt(s.start_date) if s.start_date else "Immediately",
                    ),
                    (
                        "end_date",
                        "End Date",
                        lambda: discord.utils.format_dt(s.end_date) if s.end_date else "Permanent",
                    ),
                ]
                date_fields = {"start_date", "end_date"}
                for field, label, display in special_fields:
                    if keep(field, always_show_blank=field in date_fields):
                        lines.append(f"* **{label}:** {display()}")

        return "\n".join(lines)

    async def _on_previous(self, interaction: Interaction):
        await self.menu.show_page(interaction, self.menu.current_page - 1)

    async def _on_next(self, interaction: Interaction):
        await self.menu.show_page(interaction, self.menu.current_page + 1)

    async def _on_new_suggestion(self, interaction: Interaction):
        if self.is_revamp:
            if self.type == Suggestion.Type.COUNTRYBALL:
                await interaction.response.send_modal(BallRevampSearchModal(parent_view=self))
            elif self.type == Suggestion.Type.CARD:
                await interaction.response.send_modal(CardRevampSearchModal(parent_view=self))
            else:
                await interaction.response.send_modal(EconomyRevampSearchModal(parent_view=self))
            return

        if self.type == Suggestion.Type.COUNTRYBALL:
            regimes, economies, missing_reason = await _load_countryball_requirements()
            if missing_reason is not None:
                await interaction.response.send_message(missing_reason, ephemeral=True)
                return
            await interaction.response.send_modal(BasicInfoModal(regimes, economies, parent_view=self))
        elif self.type == Suggestion.Type.CARD:
            await interaction.response.send_modal(CardBasicModal(parent_view=self))
        else:
            await interaction.response.send_modal(EconomyModal(parent_view=self))

    async def _on_select(self, interaction: Interaction):
        if not self._select_item.values or self._select_item.values[0] == NO_SUGGESTION_VALUE:
            await interaction.response.defer()
            return
        await interaction.response.defer()
        self.viewing_user = interaction.user
        self.selected = await SuggestionsService.get_detail(int(self._select_item.values[0]))
        for option in self._select_item.options:
            option.default = option.value == str(self.selected.pk)
        await self._load_art()
        await self._recompute_viewer_state()
        self._render()
        await interaction.edit_original_response(view=self, attachments=self.current_files)

    async def _on_comments(self, interaction: Interaction):
        if self.selected is None:
            await interaction.response.defer()
            return
        view = CommentsView(self.bot, self.selected.pk, self.selected.name)
        await view.initialize()
        await interaction.response.send_message(view=view, ephemeral=True)

    async def _on_upvote(self, interaction: Interaction):
        if self.selected is None:
            await interaction.response.defer()
            return
        await interaction.response.defer()
        self.viewing_user = interaction.user
        await SuggestionsService.toggle_vote(self.selected.pk, interaction.user.id)
        await self._reload_data(keep_selection=True)
        await interaction.edit_original_response(view=self, attachments=self.current_files)

    async def _on_edit_submission(self, interaction: Interaction):
        if self.selected is None:
            await interaction.response.defer()
            return
        if self.selected.status == Suggestion.Status.APPROVED:
            await interaction.response.send_message(
                "This suggestion has already been approved and can no longer be edited.", ephemeral=True
            )
            return
        if not await _can_edit(self.bot, interaction.user, self.selected):
            await interaction.response.send_message(
                "This isn't your suggestion to edit.", ephemeral=True
            )
            return
        if self.selected.type == Suggestion.Type.COUNTRYBALL:
            regimes = await SuggestionsService.list_regimes()
            economies = await SuggestionsService.list_economies()
            if not economies:
                await interaction.response.send_message(
                    "No economies are configured on this bot yet. Ask an admin to set one up before "
                    "this suggestion can be edited.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_modal(
                BasicInfoModal(regimes, economies, existing=self.selected, parent_view=self)
            )
        elif self.selected.type == Suggestion.Type.CARD:
            await interaction.response.send_modal(CardBasicModal(existing=self.selected, parent_view=self))
        else:
            await interaction.response.send_modal(EconomyModal(existing=self.selected, parent_view=self))

    async def _on_approve(self, interaction: Interaction):
        if self.selected is None:
            await interaction.response.defer()
            return
        if not await SuggestionsService.is_staff(self.bot, interaction.user):
            await interaction.response.send_message(
                "You don't have permission to review suggestions.", ephemeral=True
            )
            return
        if self.selected.status == Suggestion.Status.APPROVED:
            await interaction.response.send_message("This suggestion has already been approved.", ephemeral=True)
            return

        enabled: bool | None = None
        if self.selected.type == Suggestion.Type.COUNTRYBALL and not self.selected.is_revamp:
            collectible = settings.collectible_name
            confirm_view = ConfirmChoiceView(
                interaction,
                accept_message=f"The new {collectible} will be enabled immediately.",
                cancel_message=f"The new {collectible} will be created disabled - enable it manually once ready.",
            )
            await interaction.response.send_message(
                f"Should this {collectible} be enabled (spawnable) as soon as it's approved?",
                view=confirm_view,
                ephemeral=True,
            )
            await confirm_view.wait()
            if confirm_view.value is None:
                await interaction.followup.send("Approval cancelled - no response.", ephemeral=True)
                return
            enabled = confirm_view.value
        else:
            await interaction.response.defer()

        self.viewing_user = interaction.user
        try:
            _, warning = await SuggestionsService.approve_and_create(self.bot, self.selected.pk, enabled=enabled)
        except SuggestionApprovalError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        await self.bot.load_cache()
        await self._reload_data(keep_selection=True)
        await interaction.message.edit(view=self, attachments=self.current_files)
        if warning is not None:
            await interaction.followup.send(warning, ephemeral=True)

    async def _on_deny(self, interaction: Interaction):
        if self.selected is None:
            await interaction.response.defer()
            return
        if not await SuggestionsService.is_staff(self.bot, interaction.user):
            await interaction.response.send_message(
                "You don't have permission to review suggestions.", ephemeral=True
            )
            return
        if self.selected.status == Suggestion.Status.APPROVED:
            await interaction.response.send_message(
                "This suggestion has already been approved and can no longer be reviewed.", ephemeral=True
            )
            return
        await interaction.response.defer()
        self.viewing_user = interaction.user
        await SuggestionsService.set_status(self.selected.pk, Suggestion.Status.REJECTED)
        await self._reload_data(keep_selection=True)
        await interaction.edit_original_response(view=self, attachments=self.current_files)

    async def _on_request_changes(self, interaction: Interaction):
        if self.selected is None:
            await interaction.response.defer()
            return
        if not await SuggestionsService.is_staff(self.bot, interaction.user):
            await interaction.response.send_message(
                "You don't have permission to review suggestions.", ephemeral=True
            )
            return
        if self.selected.status == Suggestion.Status.APPROVED:
            await interaction.response.send_message(
                "This suggestion has already been approved and can no longer be reviewed.", ephemeral=True
            )
            return
        await interaction.response.send_modal(RequestChangesModal(self))
