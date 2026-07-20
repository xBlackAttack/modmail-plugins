# -*- coding: utf-8 -*-
"""
TicketStats - Modmail (modmail-dev/Modmail) için ticket istatistik plugin'i

Komutlar:
  ?tstats            -> Genel istatistikler (toplam / açık / kapalı / yanıtlanan)
  ?tstats 30         -> Son 30 günün istatistikleri
  ?tstats kapatan    -> Kim kaç ticket kapatmış (sıralı liste)
  ?tstats yanit      -> Hangi yetkili kaç ticket'a yanıt vermiş + toplam mesaj sayısı
  ?tstats mod @user  -> Belirli bir yetkilinin detaylı istatistiği

Tüm komutlara gün filtresi eklenebilir:
  ?tstats kapatan 7  -> Son 7 günde kim kaç ticket kapatmış
"""

from datetime import datetime, timedelta

import discord
from discord.ext import commands

from core import checks
from core.models import PermissionLevel

MOD_MSG_TYPES = ["thread_message", "anonymous"]


class TicketStats(commands.Cog):
    """Ticket istatistikleri: kapatma, yanıtlama ve moderatör aktivitesi."""

    def __init__(self, bot):
        self.bot = bot
        # Modmail'in log koleksiyonu (MongoDB)
        self.logs = bot.api.logs

    # ---------- yardımcılar ----------

    @staticmethod
    def _date_query(gun):
        """Son X gün için created_at filtresi üretir.
        Modmail created_at alanını str(datetime.utcnow()) formatında tutar,
        bu format lexicographic olarak karşılaştırılabilir."""
        if not gun:
            return {}, "tüm zamanlar"
        since = str(datetime.utcnow() - timedelta(days=gun))
        return {"created_at": {"$gte": since}}, f"son {gun} gün"

    @staticmethod
    def _bar(count, max_count, width=12):
        if max_count <= 0:
            return ""
        dolu = round((count / max_count) * width)
        return "█" * dolu + "░" * (width - dolu)

    # ---------- ana komut grubu ----------

    @commands.group(
        name="ticketstats",
        aliases=["tstats", "istatistik"],
        invoke_without_command=True,
    )
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def ticketstats(self, ctx, gun: int = None):
        """Genel ticket istatistikleri. Örnek: ?tstats veya ?tstats 30"""
        query, donem = self._date_query(gun)

        total = await self.logs.count_documents(query)
        acik = await self.logs.count_documents({**query, "open": True})
        kapali = await self.logs.count_documents({**query, "open": False})

        # En az bir moderatör yanıtı almış ticket sayısı
        yanitlanan = await self.logs.count_documents({
            **query,
            "messages": {
                "$elemMatch": {
                    "author.mod": True,
                    "type": {"$in": MOD_MSG_TYPES},
                }
            },
        })

        yanitsiz = total - yanitlanan
        oran = f"{(yanitlanan / total * 100):.1f}%" if total else "—"

        embed = discord.Embed(
            title=f"📊 Ticket İstatistikleri ({donem})",
            color=self.bot.main_color,
        )
        embed.add_field(name="🎫 Toplam Ticket", value=f"**{total}**", inline=True)
        embed.add_field(name="🟢 Açık", value=f"**{acik}**", inline=True)
        embed.add_field(name="🔴 Kapatılan", value=f"**{kapali}**", inline=True)
        embed.add_field(name="💬 Yanıtlanan", value=f"**{yanitlanan}**", inline=True)
        embed.add_field(name="🕳️ Hiç Yanıtlanmayan", value=f"**{yanitsiz}**", inline=True)
        embed.add_field(name="📈 Yanıtlanma Oranı", value=f"**{oran}**", inline=True)
        embed.set_footer(
            text="Detay için: ?tstats kapatan | ?tstats yanit | ?tstats mod @kişi"
        )
        await ctx.send(embed=embed)

    # ---------- kim kaç ticket kapatmış ----------

    @ticketstats.command(name="kapatan", aliases=["closers", "kapatanlar"])
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def kapatan(self, ctx, gun: int = None):
        """Kim kaç ticket kapatmış. Örnek: ?tstats kapatan 30"""
        query, donem = self._date_query(gun)
        bot_id = str(self.bot.user.id)

        pipeline = [
            {
                "$match": {
                    **query,
                    "open": False,
                    "closer": {"$ne": None},
                    "closer.id": {"$ne": bot_id},
                }
            },
            {
                "$group": {
                    "_id": "$closer.id",
                    "name": {"$last": "$closer.name"},
                    "count": {"$sum": 1},
                }
            },
            {"$sort": {"count": -1}},
            {"$limit": 20},
        ]

        sonuc = await self.logs.aggregate(pipeline).to_list(length=20)

        # Bot tarafından kapatılanlar (zaman aşımı / otomatik kapatma)
        oto_kapatma = await self.logs.count_documents(
            {**query, "open": False, "closer.id": bot_id}
        )

        if not sonuc:
            return await ctx.send("Bu dönemde yetkililer tarafından kapatılan ticket bulunamadı.")

        max_count = sonuc[0]["count"]
        toplam = sum(r["count"] for r in sonuc)

        satirlar = []
        madalyalar = ["🥇", "🥈", "🥉"]
        for i, r in enumerate(sonuc):
            rozet = madalyalar[i] if i < 3 else f"`#{i + 1:02d}`"
            isim = r.get("name") or f"<@{r['_id']}>"
            satirlar.append(
                f"{rozet} **{isim}** — `{r['count']}` ticket\n"
                f"{self._bar(r['count'], max_count)}"
            )

        embed = discord.Embed(
            title=f"🏆 Ticket Kapatma Sıralaması ({donem})",
            description="\n".join(satirlar),
            color=self.bot.main_color,
        )
        embed.set_footer(
            text=f"Listelenen toplam kapatma: {toplam} • "
            f"Otomatik kapatma (bot): {oto_kapatma}"
        )
        await ctx.send(embed=embed)

    # ---------- kim kaç ticket'a yanıt vermiş ----------

    @ticketstats.command(name="yanit", aliases=["replies", "yanıt", "yanitlar"])
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def yanit(self, ctx, gun: int = None):
        """Hangi yetkili kaç ticket'a yanıt vermiş. Örnek: ?tstats yanit 30"""
        query, donem = self._date_query(gun)

        pipeline = [
            {"$match": query} if query else {"$match": {}},
            {"$unwind": "$messages"},
            {
                "$match": {
                    "messages.author.mod": True,
                    "messages.author.id": {"$ne": str(self.bot.user.id)},
                    "messages.type": {"$in": MOD_MSG_TYPES},
                }
            },
            {
                "$group": {
                    "_id": "$messages.author.id",
                    "name": {"$last": "$messages.author.name"},
                    "mesaj": {"$sum": 1},
                    "ticketlar": {"$addToSet": "$_id"},
                }
            },
            {
                "$project": {
                    "name": 1,
                    "mesaj": 1,
                    "ticket_sayisi": {"$size": "$ticketlar"},
                }
            },
            {"$sort": {"ticket_sayisi": -1, "mesaj": -1}},
            {"$limit": 20},
        ]

        sonuc = await self.logs.aggregate(pipeline).to_list(length=20)

        if not sonuc:
            return await ctx.send("Bu dönemde moderatör yanıtı bulunamadı.")

        satirlar = []
        madalyalar = ["🥇", "🥈", "🥉"]
        for i, r in enumerate(sonuc):
            rozet = madalyalar[i] if i < 3 else f"`#{i + 1:02d}`"
            isim = r.get("name") or f"<@{r['_id']}>"
            satirlar.append(
                f"{rozet} **{isim}** — `{r['ticket_sayisi']}` ticket'a yanıt, "
                f"`{r['mesaj']}` mesaj"
            )

        embed = discord.Embed(
            title=f"💬 Yanıt Sıralaması ({donem})",
            description="\n".join(satirlar),
            color=self.bot.main_color,
        )
        await ctx.send(embed=embed)

    # ---------- tek moderatör detayı ----------

    @ticketstats.command(name="mod", aliases=["yetkili", "user"])
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def mod(self, ctx, uye: discord.Member, gun: int = None):
        """Bir yetkilinin detaylı istatistiği. Örnek: ?tstats mod @Yetkili 30"""
        query, donem = self._date_query(gun)
        uid = str(uye.id)

        kapatilan = await self.logs.count_documents(
            {**query, "open": False, "closer.id": uid}
        )

        pipeline = [
            {"$match": query} if query else {"$match": {}},
            {"$unwind": "$messages"},
            {
                "$match": {
                    "messages.author.id": uid,
                    "messages.author.mod": True,
                    "messages.type": {"$in": MOD_MSG_TYPES},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "mesaj": {"$sum": 1},
                    "ticketlar": {"$addToSet": "$_id"},
                }
            },
        ]
        sonuc = await self.logs.aggregate(pipeline).to_list(length=1)
        mesaj = sonuc[0]["mesaj"] if sonuc else 0
        yanitlanan_ticket = len(sonuc[0]["ticketlar"]) if sonuc else 0

        embed = discord.Embed(
            title=f"👤 {uye.display_name} — İstatistik ({donem})",
            color=self.bot.main_color,
        )
        embed.set_thumbnail(url=uye.display_avatar.url)
        embed.add_field(name="🔒 Kapattığı Ticket", value=f"**{kapatilan}**", inline=True)
        embed.add_field(
            name="💬 Yanıt Verdiği Ticket", value=f"**{yanitlanan_ticket}**", inline=True
        )
        embed.add_field(name="✉️ Toplam Mesaj", value=f"**{mesaj}**", inline=True)
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(TicketStats(bot))