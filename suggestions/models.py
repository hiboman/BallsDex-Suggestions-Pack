from django.db import models

from bd_models.models import Ball, BallGroup, Economy, Player, Regime, Special


class Suggestion(models.Model):
    class Type(models.TextChoices):
        COUNTRYBALL = "countryball", "Countryball"
        CARD = "card", "Card"
        ECONOMY = "economy", "Economy"

    class CardType(models.TextChoices):
        REGIME = "regime", "Regime"
        SPECIAL = "special", "Special"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        CHANGES_REQUESTED = "changes_requested", "Changes Requested"

    type = models.CharField(
        max_length=16,
        choices=Type.choices,
        default=Type.COUNTRYBALL,
        help_text="What kind of object this suggestion proposes.",
    )
    card_type = models.CharField(
        max_length=16,
        choices=CardType.choices,
        blank=True,
        null=True,
        help_text='Only set when Type is "Card": whether this proposes a new Regime or a new Special event.',
    )

    is_revamp = models.BooleanField(default=False)
    revamp_ball = models.ForeignKey(
        Ball,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="The existing Ball this suggestion proposes revamping.",
    )
    revamp_regime = models.ForeignKey(
        Regime,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="The existing Regime this suggestion proposes revamping.",
    )
    revamp_special = models.ForeignKey(
        Special,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="The existing Special event this suggestion proposes revamping.",
    )
    revamp_economy = models.ForeignKey(
        Economy,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="The existing Economy this suggestion proposes revamping.",
    )

    submitted_by = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="ball_suggestions")

    name = models.CharField(max_length=64, verbose_name="Name")

    short_name = models.CharField(
        max_length=24,
        blank=True,
        null=True,
        help_text=(
            "An alternative shorter name, used only when generating the card if the full name is "
            "too long."
        ),
    )
    catch_names = models.TextField(
        blank=True,
        null=True,
        help_text="Additional possible names to catch this ball by, separated by semicolons.",
    )
    regime = models.ForeignKey(
        Regime,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="Suggested political regime",
    )
    economy = models.ForeignKey(
        Economy,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="Suggested economical regime",
    )
    group = models.ForeignKey(
        BallGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Suggested existing group this ball should belong to.",
    )
    health = models.IntegerField(blank=True, null=True, help_text="Suggested health stat")
    attack = models.IntegerField(blank=True, null=True, help_text="Suggested attack stat")
    capacity_name = models.CharField(
        max_length=64, blank=True, null=True, help_text="Name of the suggested capability"
    )
    capacity_description = models.CharField(
        max_length=256, blank=True, null=True, help_text="Description of the suggested capability"
    )
    spawn_art = models.ImageField(max_length=200, blank=True, null=True)
    window_art = models.ImageField(
        max_length=200, blank=True, null=True, help_text="Image used when displaying the ball"
    )
    emoji_art = models.ImageField(
        max_length=200,
        blank=True,
        null=True,
        help_text="Optional image for the Discord emoji. If left blank, the emoji is auto-generated from Spawn Art instead.",
    )

    card_background = models.ImageField(
        max_length=200, blank=True, null=True, help_text="1428x2000 PNG background art"
    )
    catch_phrase = models.CharField(
        max_length=128,
        blank=True,
        null=True,
        help_text="Sentence sent in bonus when someone catches a card of this event",
    )
    emoji = models.CharField(
        max_length=16,
        blank=True,
        null=True,
        help_text="Unicode emoji shown next to this event's name.",
    )
    start_date = models.DateTimeField(
        blank=True, null=True, help_text="Suggested event start time. Blank starts immediately"
    )
    end_date = models.DateTimeField(
        blank=True, null=True, help_text="Suggested event end time. Blank runs permanently"
    )

    economy_icon = models.ImageField(
        max_length=200, blank=True, null=True, help_text="512x512 PNG icon image"
    )

    rarity = models.FloatField(
        blank=True,
        null=True,
        help_text=(
            "Suggested rarity (Countryball), or chances of this event's background being used (Special card)."
        ),
    )

    credits = models.CharField(max_length=64, blank=True, null=True, help_text="Artwork credits")

    status = models.CharField(max_length=32, choices=Status.choices, default=Status.PENDING)
    admin_comment = models.CharField(max_length=256, blank=True, null=True, verbose_name="Staff Note")

    created_at = models.DateTimeField(auto_now_add=True)

    objects: models.Manager["Suggestion"] = models.Manager()

    class Meta:
        managed = True
        db_table = "ball_suggestion"
        ordering = ("-created_at",)
        verbose_name = "suggestion"
        verbose_name_plural = "suggestions"

    def __str__(self) -> str:
        return self.name


class SuggestionVote(models.Model):
    suggestion = models.ForeignKey(Suggestion, on_delete=models.CASCADE, related_name="votes")
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="suggestion_votes")
    created_at = models.DateTimeField(auto_now_add=True)

    objects: models.Manager["SuggestionVote"] = models.Manager()

    class Meta:
        managed = True
        db_table = "ball_suggestion_vote"
        constraints = [
            models.UniqueConstraint(fields=("suggestion", "player"), name="unique_suggestion_vote"),
        ]

    def __str__(self) -> str:
        return f"{self.player} -> {self.suggestion}"


class SuggestionComment(models.Model):
    suggestion = models.ForeignKey(Suggestion, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="suggestion_comments")
    content = models.CharField(max_length=1000, help_text="The comment text.")
    created_at = models.DateTimeField(auto_now_add=True)

    objects: models.Manager["SuggestionComment"] = models.Manager()

    class Meta:
        managed = True
        db_table = "ball_suggestion_comment"
        ordering = ("created_at",)

    def __str__(self) -> str:
        return f"{self.author} on {self.suggestion}: {self.content[:32]!r}"
