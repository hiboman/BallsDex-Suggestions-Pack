import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("bd_models", "0017_ballgroup"),
    ]

    operations = [
        migrations.CreateModel(
            name="Suggestion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "type",
                    models.CharField(
                        choices=[("countryball", "Countryball"), ("card", "Card"), ("economy", "Economy")],
                        default="countryball",
                        help_text="What kind of real BallsDex object this suggestion proposes.",
                        max_length=16,
                    ),
                ),
                (
                    "card_type",
                    models.CharField(
                        blank=True,
                        choices=[("regime", "Regime"), ("special", "Special")],
                        help_text=(
                            'Only set when Type is "Card": whether this proposes a new Regime or a new '
                            "Special event."
                        ),
                        max_length=16,
                        null=True,
                    ),
                ),
                (
                    "is_revamp",
                    models.BooleanField(
                        default=False,
                        help_text=(
                            "If set, this suggestion proposes replacing an existing "
                            "Ball/Regime/Special/Economy's data (via whichever `revamp_*` field below is "
                            "set) instead of creating a new one."
                        ),
                    ),
                ),
                (
                    "revamp_ball",
                    models.ForeignKey(
                        blank=True,
                        help_text="The existing Ball this suggestion proposes revamping (Countryball revamp only).",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="bd_models.ball",
                    ),
                ),
                (
                    "revamp_regime",
                    models.ForeignKey(
                        blank=True,
                        help_text="The existing Regime this suggestion proposes revamping (Regime card revamp only).",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="bd_models.regime",
                    ),
                ),
                (
                    "revamp_special",
                    models.ForeignKey(
                        blank=True,
                        help_text=(
                            "The existing Special event this suggestion proposes revamping (Special "
                            "card revamp only)."
                        ),
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="bd_models.special",
                    ),
                ),
                (
                    "revamp_economy",
                    models.ForeignKey(
                        blank=True,
                        help_text="The existing Economy this suggestion proposes revamping (Economy revamp only).",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="bd_models.economy",
                    ),
                ),
                ("name", models.CharField(max_length=64, verbose_name="Name")),
                (
                    "short_name",
                    models.CharField(
                        blank=True,
                        help_text=(
                            "An alternative shorter name, used only when generating the card if the full "
                            "name is too long (Countryball only)."
                        ),
                        max_length=24,
                        null=True,
                    ),
                ),
                (
                    "catch_names",
                    models.TextField(
                        blank=True,
                        help_text=(
                            "Additional possible names to catch this ball by, separated by semicolons "
                            "(Countryball only)."
                        ),
                        null=True,
                    ),
                ),
                (
                    "health",
                    models.IntegerField(
                        blank=True, help_text="Suggested health (HP) stat (Countryball only)", null=True
                    ),
                ),
                (
                    "attack",
                    models.IntegerField(
                        blank=True, help_text="Suggested attack (ATK) stat (Countryball only)", null=True
                    ),
                ),
                (
                    "capacity_name",
                    models.CharField(
                        blank=True,
                        help_text="Name of the suggested capability (Countryball only)",
                        max_length=64,
                        null=True,
                    ),
                ),
                (
                    "capacity_description",
                    models.CharField(
                        blank=True,
                        help_text="Description of the suggested capability (Countryball only)",
                        max_length=256,
                        null=True,
                    ),
                ),
                (
                    "spawn_art",
                    models.ImageField(
                        blank=True,
                        help_text="Image used when the ball spawns in the wild (Countryball only)",
                        max_length=200,
                        null=True,
                        upload_to="",
                    ),
                ),
                (
                    "window_art",
                    models.ImageField(
                        blank=True,
                        help_text="Image used when displaying the ball (Countryball only)",
                        max_length=200,
                        null=True,
                        upload_to="",
                    ),
                ),
                (
                    "emoji_art",
                    models.ImageField(
                        blank=True,
                        help_text=(
                            "Optional dedicated image for the Discord emoji (Countryball only). If left "
                            "blank, the emoji is auto-generated from Spawn Art instead - see "
                            "SuggestionsService._create_emoji_for."
                        ),
                        max_length=200,
                        null=True,
                        upload_to="",
                    ),
                ),
                (
                    "card_background",
                    models.ImageField(
                        blank=True,
                        help_text="1428x2000 PNG background art (Card only)",
                        max_length=200,
                        null=True,
                        upload_to="",
                    ),
                ),
                (
                    "catch_phrase",
                    models.CharField(
                        blank=True,
                        help_text="Sentence sent in bonus when someone catches a card of this event (Special card only)",
                        max_length=128,
                        null=True,
                    ),
                ),
                (
                    "emoji",
                    models.CharField(
                        blank=True,
                        help_text=(
                            "Unicode emoji shown next to this event's name (Special card only) - the "
                            "emoji itself, not its ID."
                        ),
                        max_length=16,
                        null=True,
                    ),
                ),
                (
                    "start_date",
                    models.DateTimeField(
                        blank=True,
                        help_text="Suggested event start time. Blank starts immediately (Special card only)",
                        null=True,
                    ),
                ),
                (
                    "end_date",
                    models.DateTimeField(
                        blank=True,
                        help_text="Suggested event end time. Blank runs permanently (Special card only)",
                        null=True,
                    ),
                ),
                (
                    "economy_icon",
                    models.ImageField(
                        blank=True,
                        help_text="512x512 PNG icon image (Economy only)",
                        max_length=200,
                        null=True,
                        upload_to="",
                    ),
                ),
                (
                    "rarity",
                    models.FloatField(
                        blank=True,
                        help_text=(
                            "Suggested rarity (Countryball), or chances of this event's background being "
                            "used (Special card)."
                        ),
                        null=True,
                    ),
                ),
                (
                    "credits",
                    models.CharField(blank=True, help_text="Artwork credits", max_length=64, null=True),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("approved", "Approved"),
                            ("rejected", "Rejected"),
                            ("changes_requested", "Changes Requested"),
                        ],
                        default="pending",
                        max_length=32,
                    ),
                ),
                (
                    "admin_comment",
                    models.CharField(
                        blank=True,
                        help_text="Optional note left by staff when reviewing",
                        max_length=256,
                        null=True,
                        verbose_name="Staff Note",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "regime",
                    models.ForeignKey(
                        blank=True,
                        help_text="Suggested political regime (Countryball only)",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="bd_models.regime",
                    ),
                ),
                (
                    "economy",
                    models.ForeignKey(
                        blank=True,
                        help_text="Suggested economical regime (Countryball only)",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="bd_models.economy",
                    ),
                ),
                (
                    "group",
                    models.ForeignKey(
                        blank=True,
                        help_text=(
                            'Suggested existing group this ball should belong to, e.g. "Cities" '
                            "(Countryball only)."
                        ),
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="bd_models.ballgroup",
                    ),
                ),
                (
                    "submitted_by",
                    models.ForeignKey(
                        help_text="Player who submitted this suggestion",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ball_suggestions",
                        to="bd_models.player",
                    ),
                ),
            ],
            options={
                "verbose_name": "suggestion",
                "verbose_name_plural": "suggestions",
                "db_table": "ball_suggestion",
                "ordering": ("-created_at",),
            },
        ),
        migrations.CreateModel(
            name="SuggestionVote",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "player",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="suggestion_votes",
                        to="bd_models.player",
                    ),
                ),
                (
                    "suggestion",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="votes",
                        to="suggestions.suggestion",
                    ),
                ),
            ],
            options={
                "db_table": "ball_suggestion_vote",
            },
        ),
        migrations.AddConstraint(
            model_name="suggestionvote",
            constraint=models.UniqueConstraint(fields=("suggestion", "player"), name="unique_suggestion_vote"),
        ),
        migrations.CreateModel(
            name="SuggestionComment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("content", models.CharField(help_text="The comment text.", max_length=1000)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "author",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="suggestion_comments",
                        to="bd_models.player",
                    ),
                ),
                (
                    "suggestion",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="comments",
                        to="suggestions.suggestion",
                    ),
                ),
            ],
            options={
                "db_table": "ball_suggestion_comment",
                "ordering": ("created_at",),
            },
        ),
    ]
