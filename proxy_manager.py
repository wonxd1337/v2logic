# proxy_manager.py
import threading
import time
import random
import requests
from queue import Queue
from collections import defaultdict
from config import Config

class ProxyManager:
    def __init__(self):
        self.proxy_list = []
        self.socks5_proxies = []  # List khusus SOCKS5
        self.http_proxies = []    # List HTTP/HTTPS
        self.proxy_stats = defaultdict(lambda: {
            'success': 0, 'fail': 0, 'total_time': 0,
            'avg_time': 1.0, 'weight': 1.0, 'last_used': 0,
            'proxy_type': 'http'  # http or socks5
        })
        self.lock = threading.Lock()
        self.running = True
        self.last_refresh = 0
        
        # Weight settings
        self.min_weight = 0.1
        self.max_weight = 3.0
        
        # SOCKS5 priority (higher = prefer SOCKS5)
        self.socks5_priority = 0.7  # 70% chance to use SOCKS5 if available
        
        # Mulai thread auto-refresh
        self.start_auto_refresh()
    
    def parse_proxy_string(self, proxy_str):
        """Parse proxy string dan deteksi tipe"""
        proxy_str = proxy_str.strip()
        
        # Deteksi SOCKS5
        if 'socks5://' in proxy_str.lower():
            return {'url': proxy_str, 'type': 'socks5'}
        elif 'socks4://' in proxy_str.lower():
            return {'url': proxy_str, 'type': 'socks4'}
        elif 'http://' in proxy_str.lower() or 'https://' in proxy_str.lower():
            return {'url': proxy_str, 'type': 'http'}
        else:
            # Default ke HTTP jika tidak ada prefix
            return {'url': f'http://{proxy_str}', 'type': 'http'}
    
    def download_proxies(self):
        """Download proxy list dari GitHub dan pisahkan berdasarkan tipe"""
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            response = requests.get(
                Config.PROXY_URL, 
                headers=headers, 
                timeout=30, 
                verify=False
            )
            
            if response.status_code == 200:
                proxies = [line.strip() for line in response.text.split('\n') if line.strip()]
                
                # Pisahkan SOCKS5 dan HTTP proxies
                socks5_temp = []
                http_temp = []
                
                for proxy in proxies:
                    parsed = self.parse_proxy_string(proxy)
                    if parsed['type'] == 'socks5':
                        socks5_temp.append(parsed['url'])
                    elif parsed['type'] == 'socks4':
                        socks5_temp.append(parsed['url'])  # Treat SOCKS4 as SOCKS5
                    else:
                        http_temp.append(parsed['url'])
                
                with self.lock:
                    # Update proxy lists
                    old_proxies = set(self.proxy_list)
                    new_proxies = set(proxies)
                    
                    # Hapus proxy yang tidak ada di list baru
                    for proxy in old_proxies - new_proxies:
                        if proxy in self.proxy_stats:
                            del self.proxy_stats[proxy]
                    
                    # Update lists
                    self.socks5_proxies = socks5_temp
                    self.http_proxies = http_temp
                    self.proxy_list = proxies
                    
                    # Initialize stats for new proxies
                    for proxy in new_proxies - old_proxies:
                        parsed = self.parse_proxy_string(proxy)
                        self.proxy_stats[proxy] = {
                            'success': 0, 'fail': 0, 'total_time': 0,
                            'avg_time': 1.0, 'weight': 1.0, 'last_used': 0,
                            'proxy_type': parsed['type']
                        }
                
                print(f"[+] Proxy Manager: {len(self.socks5_proxies)} SOCKS5, {len(self.http_proxies)} HTTP proxies loaded")
                return proxies
                
        except Exception as e:
            print(f"[-] Proxy download error: {str(e)[:50]}")
            return self.proxy_list
    
    def refresh_proxies(self):
        """Refresh proxy list"""
        while self.running:
            time.sleep(Config.PROXY_REFRESH_INTERVAL)
            print("\n[*] Refreshing proxy list...")
            new_proxies = self.download_proxies()
            
            # Bersihkan statistik lama jika perlu
            with self.lock:
                if len(self.proxy_stats) > Config.MAX_CACHE_SIZE:
                    # Hapus proxy dengan performa terburuk
                    sorted_proxies = sorted(
                        self.proxy_stats.items(),
                        key=lambda x: x[1]['weight']
                    )
                    for proxy, _ in sorted_proxies[:len(self.proxy_stats)//2]:
                        del self.proxy_stats[proxy]
            
            print(f"[+] Proxy refreshed: {len(self.socks5_proxies)} SOCKS5, {len(self.http_proxies)} HTTP")
    
    def start_auto_refresh(self):
        """Mulai thread auto-refresh"""
        # Download initial
        self.download_proxies()
        
        # Start refresh thread
        refresh_thread = threading.Thread(target=self.refresh_proxies, daemon=True)
        refresh_thread.start()
    
    def update_stats(self, proxy, success, response_time=None):
        """Update statistik proxy"""
        if proxy not in self.proxy_stats:
            return
            
        with self.lock:
            stats = self.proxy_stats[proxy]
            stats['last_used'] = time.time()
            
            if success:
                stats['success'] += 1
                if response_time:
                    total = stats['total_time'] + response_time
                    count = stats['success'] + stats['fail']
                    stats['avg_time'] = total / count if count > 0 else response_time
                    stats['total_time'] = total
                    
                # Bonus weight untuk SOCKS5 yang sukses
                if stats['proxy_type'] == 'socks5':
                    stats['weight'] *= 1.1  # 10% bonus untuk SOCKS5
            else:
                stats['fail'] += 1
                # Penalty lebih kecil untuk SOCKS5 (lebih tahan lama)
                if stats['proxy_type'] == 'socks5':
                    stats['weight'] *= 0.8
                else:
                    stats['weight'] *= 0.5
            
            # Hitung ulang bobot
            total_reqs = stats['success'] + stats['fail']
            if total_reqs > 0:
                success_rate = stats['success'] / total_reqs
                speed_score = 1.0 / stats['avg_time'] if stats['avg_time'] > 0 else 1.0
                speed_score = min(speed_score, 2.0)
                
                weight = (success_rate * 0.6 + speed_score * 0.4) * 2
                stats['weight'] = max(self.min_weight, min(self.max_weight, weight))
                
                # SOCKS5 additional bonus
                if stats['proxy_type'] == 'socks5':
                    stats['weight'] = min(self.max_weight, stats['weight'] * 1.2)
    
    def get_proxy(self, prefer_socks5=True):
        """Dapatkan proxy dengan prioritas SOCKS5"""
        with self.lock:
            if not self.proxy_list:
                return None
            
            # Prioritaskan SOCKS5 jika ada dan prefer_socks5=True
            if prefer_socks5 and self.socks5_proxies:
                # 70% chance to use SOCKS5 if available
                if random.random() < self.socks5_priority:
                    # Pilih dari SOCKS5 proxies dengan weighted selection
                    socks5_with_stats = [
                        p for p in self.socks5_proxies 
                        if p in self.proxy_stats
                    ]
                    
                    if socks5_with_stats:
                        total_weight = sum(self.proxy_stats[p]['weight'] for p in socks5_with_stats)
                        if total_weight > 0:
                            r = random.uniform(0, total_weight)
                            cumulative = 0
                            for proxy in socks5_with_stats:
                                cumulative += self.proxy_stats[proxy]['weight']
                                if r <= cumulative:
                                    return {"http": proxy, "https": proxy}
                    
                    # Fallback ke random SOCKS5
                    proxy = random.choice(self.socks5_proxies)
                    return {"http": proxy, "https": proxy}
            
            # Jika tidak ada SOCKS5 atau prefer_socks5=False, gunakan HTTP
            http_with_stats = [
                p for p in self.http_proxies 
                if p in self.proxy_stats
            ]
            
            if http_with_stats:
                total_weight = sum(self.proxy_stats[p]['weight'] for p in http_with_stats)
                if total_weight > 0:
                    r = random.uniform(0, total_weight)
                    cumulative = 0
                    for proxy in http_with_stats:
                        cumulative += self.proxy_stats[proxy]['weight']
                        if r <= cumulative:
                            return {"http": proxy, "https": proxy}
            
            # Final fallback
            if self.proxy_list:
                proxy = random.choice(self.proxy_list)
                return {"http": proxy, "https": proxy}
            
            return None
    
    def get_different_proxy(self, last_proxy=None):
        """Dapatkan proxy yang berbeda dari yang terakhir digunakan"""
        with self.lock:
            if not self.proxy_list:
                return None
            
            available = [p for p in self.proxy_list if p != last_proxy]
            if not available:
                available = self.proxy_list
            
            # Prioritaskan SOCKS5
            socks5_available = [p for p in available if p in self.socks5_proxies]
            if socks5_available and random.random() < self.socks5_priority:
                proxy = random.choice(socks5_available)
                return {"http": proxy, "https": proxy}
            
            proxy = random.choice(available)
            return {"http": proxy, "https": proxy}
    
    def get_proxy_for_retry(self, retry_count, last_proxy=None):
        """Dapatkan proxy untuk retry, dengan jaminan berbeda setiap retry"""
        with self.lock:
            if not self.proxy_list:
                return None
            
            # Filter out last proxy if needed
            available = self.proxy_list
            if last_proxy:
                available = [p for p in self.proxy_list if p != last_proxy]
            
            if not available:
                available = self.proxy_list
            
            # Untuk retry awal, prioritaskan SOCKS5
            if retry_count < 2:
                socks5_available = [p for p in available if p in self.socks5_proxies]
                if socks5_available:
                    proxy = random.choice(socks5_available)
                    return {"http": proxy, "https": proxy}
            
            # Untuk retry berikutnya, pilih random
            proxy = random.choice(available)
            return {"http": proxy, "https": proxy}
    
    def print_stats(self):
        """Tampilkan statistik proxy"""
        print("\n" + "="*60)
        print("PROXY STATISTICS (Top 10)")
        print("="*60)
        print(f"📡 SOCKS5 Available: {len(self.socks5_proxies)} | HTTP: {len(self.http_proxies)}")
        print("-"*60)
        
        with self.lock:
            # Sort by weight
            sorted_proxies = sorted(
                [(p, s) for p, s in self.proxy_stats.items() if p in self.proxy_list],
                key=lambda x: x[1]['weight'],
                reverse=True
            )[:10]
            
            for proxy, stats in sorted_proxies:
                total = stats['success'] + stats['fail']
                if total > 0:
                    success_rate = (stats['success'] / total) * 100
                    proxy_type_icon = "🔒" if stats['proxy_type'] == 'socks5' else "🌐"
                    print(f"{proxy_type_icon} {proxy[:60]}")
                    print(f"  ✅ Success: {stats['success']} | ❌ Fail: {stats['fail']}")
                    print(f"  📊 Rate: {success_rate:.1f}% | Time: {stats['avg_time']:.2f}s")
                    print(f"  ⚖️ Weight: {stats['weight']:.2f}")
                    print()
    
    def cleanup(self):
        """Bersihkan resources"""
        self.running = False