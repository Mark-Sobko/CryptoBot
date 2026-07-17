import feedparser
import logging
import re
from textblob import TextBlob # Библиотека для анализа тональности
from typing import Dict, Union

class NewsFilter:
    """
    [INSTITUTIONAL MACRO GUARD v2.0]
    Модуль новостного фильтра:
    - Расширенные макро-триггеры (NFP, Powell, SEC)
    - Очистка HTML-мусора для точного NLP
    - Безопасное извлечение ID
    """
    def __init__(self):
        self.logger = logging.getLogger("SMC_BOT.NewsFilter")
        self.rss_url = "https://www.investing.com/rss/news_25.rss"
        self.last_news_id = None
        
        # Расширенный словарь институциональных красных флагов
        self.critical_keywords = [
            "fomc", "cpi", "rate decision", "nfp", "powell",
            "gdp", "inflation", "sec", "lawsuit", "bankruptcy",
            "hacked", "investigation", "fed", "interest rate",
            "nonfarm"
        ]

    def _fetch_news(self) -> list:
        try:
            feed = feedparser.parse(self.rss_url)
            
            # Защита от криво спарсенных фидов
            if getattr(feed, 'bozo', 0) == 1 and isinstance(getattr(feed, 'bozo_exception', None), Exception):
                self.logger.warning(f"⚠️ [RSS WARNING] Feed parsing issue: {feed.bozo_exception}")
                
            return feed.entries
        except Exception as e:
            self.logger.error(f"❌ [RSS ERROR] Failed to fetch news: {e}")
            return []

    def _clean_text(self, text: str) -> str:
        """Очистка заголовков от возможных HTML-тегов для точного NLP-анализа."""
        clean = re.sub(r'<.*?>', '', str(text))
        return clean.replace("&amp;", "&").replace("&quot;", '"').strip()

    def analyze_news(self) -> Dict[str, Union[str, float]]:
        """
        Возвращает: {'action': 'BLOCK'/'LONG'/'SHORT'/'NONE', 'title': str, 'score': float, 'published': str}
        """
        entries = self._fetch_news()
        if not entries:
            return {"action": "NONE", "title": "", "score": 0.0, "published": ""}

        latest = entries[0] # Берем самую свежую новость
        
        # Безопасное получение ID (в некоторых фидах id может отсутствовать)
        news_id = getattr(latest, 'id', getattr(latest, 'link', 'unknown_id'))
        raw_title = getattr(latest, 'title', '')
        published = getattr(latest, 'published', '')
        
        if not raw_title or self.last_news_id == news_id:
            return {"action": "NONE", "title": "", "score": 0.0, "published": ""}
        
        self.last_news_id = news_id
        clean_title = self._clean_text(raw_title)
        
        # Анализ тональности
        analysis = TextBlob(clean_title)
        polarity = analysis.sentiment.polarity # От -1 (негатив) до 1 (позитив)
        
        # Определение значимости (институциональные триггеры)
        title_lower = clean_title.lower()
        
        # Проверка на наличие макроэкономических маркеров
        if any(kw in title_lower for kw in self.critical_keywords):
            self.logger.warning(f"🚨 [MACRO BLOCK] High-impact news detected: {clean_title}")
            return {"action": "BLOCK", "title": clean_title, "score": float(polarity), "published": published}
        
        if polarity > 0.3: # Явный позитив
            self.logger.info(f"📈 [MACRO LONG] Positive sentiment: {clean_title} (Score: {polarity:.2f})")
            return {"action": "LONG", "title": clean_title, "score": float(polarity), "published": published}
            
        elif polarity < -0.3: # Явный негатив
            self.logger.warning(f"📉 [MACRO SHORT] Negative sentiment: {clean_title} (Score: {polarity:.2f})")
            return {"action": "SHORT", "title": clean_title, "score": float(polarity), "published": published}
            
        return {"action": "NONE", "title": clean_title, "score": float(polarity), "published": published}