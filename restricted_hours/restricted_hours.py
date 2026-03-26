import discord
from discord.ext import commands
from datetime import datetime, timezone, timedelta

# ─── AYARLAR ────────────────────────────────────────────────────────────────

EXEMPT_ROLE_ID = 1407104983672950986

ALLOWED_START_HOUR = 15   # 15:00
ALLOWED_END_HOUR   = 18   # 18:00 (dahil değil, yani 17:59'a kadar)

TURKEY_TZ = timezone(timedelta(hours=3))

MSG_OUTSIDE_HOURS = (
    "🕐 **Şu anda hizmet vermiyoruz.**\n\n"
    "Destek ekibimize **her gün saat 15:00 – 18:00** (Türkiye saati) arasında ulaşabilirsiniz.\n"
    "Bu saatler dışında gönderilen mesajlar işleme alınmaz.\n\n"
    "Anlayışınız için teşekkür ederiz. 🙏"
)

# ─────────────────────────────────────────────────────────────────────────────


class RestrictedHours(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _is_within_allowed_hours(self) -> bool:
        now = datetime.now(TURKEY_TZ)
        return ALLOWED_START_HOUR <= now.hour < ALLOWED_END_HOUR

    def _user_has_exempt_role(self, member) -> bool:
        if member is None:
            return False
        return any(role.id == EXEMPT_ROLE_ID for role in member.roles)

    def _welcome_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="Destek talebi oluşturuldu.",
            description="Mesajın bize ulaştı. Admin ekibi en kısa sürede sizinle iletişime geçecek.",
            color=discord.Color.green(),
        )
        return embed

    @commands.Cog.listener()
    async def on_thread_ready(self, thread, creator, category, initial_message):
        user = thread.recipient
        if user is None:
            return

        guild = self.bot.modmail_guild
        try:
            member = guild.get_member(user.id) or await guild.fetch_member(user.id)
        except Exception:
            member = None

        # Muaf role sahipse → welcome embed gönder, devam et
        if self._user_has_exempt_role(member):
            try:
                await user.send(embed=self._welcome_embed())
            except Exception:
                pass
            return

        # Uygun saatteyse → welcome embed gönder, devam et
        if self._is_within_allowed_hours():
            try:
                await user.send(embed=self._welcome_embed())
            except Exception:
                pass
            return

        # ── Kısıtlama devreye giriyor ──
        try:
            await user.send(MSG_OUTSIDE_HOURS)
        except Exception:
            pass

        try:
            await thread.close(
                closer=self.bot.user,
                silent=True,
                delete_channel=True,
                message=f"Mesaj çalışma saatleri ({ALLOWED_START_HOUR}:00–{ALLOWED_END_HOUR}:00) dışında gönderildi.",
            )
        except Exception:
            try:
                await thread.channel.delete()
            except Exception:
                pass


async def setup(bot):
    await bot.add_cog(RestrictedHours(bot))