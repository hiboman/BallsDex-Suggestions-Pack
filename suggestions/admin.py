from typing import Any

from django.contrib import admin
from django.http import HttpRequest
from django.utils.safestring import SafeText, mark_safe

from .models import Suggestion, SuggestionComment, SuggestionVote


def _image_preview(image_field: Any) -> SafeText:
    if not image_field:
        return mark_safe("<em>No file uploaded</em>")
    return mark_safe(f'<img src="/{image_field.url}" width="80%" />')


class SuggestionVoteInline(admin.TabularInline):
    model = SuggestionVote
    extra = 0
    fields = ("player", "created_at")
    readonly_fields = ("created_at",)
    autocomplete_fields = ("player",)
    ordering = ("-created_at",)


class SuggestionCommentInline(admin.StackedInline):
    model = SuggestionComment
    extra = 0
    fields = ("author", "content", "created_at")
    readonly_fields = ("created_at",)
    autocomplete_fields = ("author",)
    ordering = ("created_at",)


@admin.register(Suggestion)
class SuggestionAdmin(admin.ModelAdmin):
    autocomplete_fields = (
        "regime",
        "economy",
        "group",
        "submitted_by",
        "revamp_ball",
        "revamp_regime",
        "revamp_special",
        "revamp_economy",
    )
    readonly_fields = (
        "spawn_image",
        "window_image",
        "emoji_image",
        "card_background_image",
        "economy_icon_image",
        "created_at",
    )
    inlines = (SuggestionVoteInline, SuggestionCommentInline)
    save_on_top = True

    fieldsets = [
        (None, {"fields": ["type", "name"]}),
        (
            "Moderation",
            {
                "fields": ["status", "admin_comment", "submitted_by", "created_at"],
            },
        ),
        (
            "Revamp",
            {
                "fields": ["is_revamp", "revamp_ball", "revamp_regime", "revamp_special", "revamp_economy"],
            },
        ),
        (
            "Countryball",
            {
                "description": "Only used when Type is Countryball.",
                "fields": ["regime", "economy", "group", "health", "attack", "short_name", "catch_names"],
            },
        ),
        (
            "Countryball Assets",
            {
                "description": "You must have permission from the copyright holder to use the files you're uploading!",
                "fields": ["spawn_image", "spawn_art", "window_image", "window_art", "emoji_image", "emoji_art"],
            },
        ),
        (
            "Countryball Ability",
            {"fields": ["capacity_name", "capacity_description"]},
        ),
        (
            "Card",
            {
                "description": (
                    'Only used when Type is Card. Card Type picks "Regime" or "Special"; the '
                    "Special-only fields below (Emoji, Catch Phrase, Start/End Date) are ignored for a Regime."
                ),
                "fields": [
                    "card_type",
                    "card_background_image",
                    "card_background",
                    "emoji",
                    "catch_phrase",
                    "start_date",
                    "end_date",
                ],
            },
        ),
        (
            "Economy",
            {
                "description": "Only used when Type is Economy.",
                "fields": ["economy_icon_image", "economy_icon"],
            },
        ),
        (
            "Shared",
            {
                "description": "Applies only to Countryballs and Special Cards",
                "fields": ["rarity", "credits"],
            },
        ),
    ]

    list_display = ("name", "type", "card_type", "is_revamp", "status", "submitted_by", "created_at")
    list_filter = ("type", "card_type", "is_revamp", "status", "regime", "economy", "group")
    search_fields = ("name", "short_name", "catch_names")
    ordering = ("-created_at",)

    def get_fieldsets(self, request: HttpRequest, obj: Suggestion | None = None):
        if obj is None:
            return self.fieldsets

        hidden_sections: set[str] = set()
        if obj.type != Suggestion.Type.COUNTRYBALL:
            hidden_sections |= {"Countryball", "Countryball Assets", "Countryball Ability"}
        if obj.type != Suggestion.Type.CARD:
            hidden_sections |= {"Card"}
        if obj.type != Suggestion.Type.ECONOMY:
            hidden_sections |= {"Economy"}

        if obj.type == Suggestion.Type.COUNTRYBALL:
            keep_revamp_fields = {"revamp_ball"}
        elif obj.type == Suggestion.Type.ECONOMY:
            keep_revamp_fields = {"revamp_economy"}
        elif obj.type == Suggestion.Type.CARD:
            if obj.card_type == Suggestion.CardType.SPECIAL:
                keep_revamp_fields = {"revamp_special"}
            elif obj.card_type == Suggestion.CardType.REGIME:
                keep_revamp_fields = {"revamp_regime"}
            else:
                keep_revamp_fields = {"revamp_regime", "revamp_special"}
        else:
            keep_revamp_fields = {"revamp_ball", "revamp_regime", "revamp_special", "revamp_economy"}

        fieldsets = []
        for name, options in self.fieldsets:
            if name in hidden_sections:
                continue
            if name == "Revamp":
                options = {
                    **options,
                    "fields": [
                        field
                        for field in options["fields"]
                        if field == "is_revamp" or field in keep_revamp_fields
                    ],
                }
            fieldsets.append((name, options))
        return fieldsets

    def get_readonly_fields(self, request: HttpRequest, obj: Suggestion | None = None):
        if obj is None or obj.status != Suggestion.Status.APPROVED:
            return self.readonly_fields

        locked_fields = {
            field
            for fieldset_name, fieldset in self.fieldsets
            if fieldset_name != "Moderation"
            for field in fieldset["fields"]
        }
        return tuple(locked_fields | set(self.readonly_fields))

    @admin.display(description="Current spawn asset")
    def spawn_image(self, obj: Suggestion) -> SafeText:
        return _image_preview(obj.spawn_art)

    @admin.display(description="Current window card")
    def window_image(self, obj: Suggestion) -> SafeText:
        return _image_preview(obj.window_art)

    @admin.display(description="Current emoji art")
    def emoji_image(self, obj: Suggestion) -> SafeText:
        return _image_preview(obj.emoji_art)

    @admin.display(description="Current background art")
    def card_background_image(self, obj: Suggestion) -> SafeText:
        return _image_preview(obj.card_background)

    @admin.display(description="Current icon")
    def economy_icon_image(self, obj: Suggestion) -> SafeText:
        return _image_preview(obj.economy_icon)
