import asyncio
import logging
import time
from typing import Optional, Callable, Any
from telethon import TelegramClient
from telethon.errors import (
    NetworkMigrateError, FloodWaitError, 
    AuthKeyDuplicatedError, AuthKeyUnregisteredError
)

logger = logging.getLogger(__name__)

class ConnectionManager:
    """
    Manages Telegram client connections with robust retry logic and error handling.
    Provides connection monitoring, automatic reconnection, and exponential backoff.
    """
    
    def __init__(self, client: TelegramClient, name: str = "TelegramClient", is_bot: bool = False):
        self.client = client
        self.name = name
        self.is_bot = is_bot  # Flag to distinguish between bot and user clients
        self.max_retries = 10
        self.base_delay = 1.0
        self.max_delay = 300.0  # 5 minutes
        self.connection_timeout = 30.0
        self.is_running = False
        self.retry_count = 0
        self.last_error = None
        self.connection_callbacks = []
        
    def add_connection_callback(self, callback: Callable[[str, bool], None]):
        """Add callback to be called on connection state changes."""
        self.connection_callbacks.append(callback)
        
    def _notify_connection_state(self, connected: bool):
        """Notify callbacks about connection state changes."""
        for callback in self.connection_callbacks:
            try:
                callback(self.name, connected)
            except Exception as e:
                logger.error(f"Error in connection callback: {e}")
    
    def _calculate_delay(self) -> float:
        """Calculate exponential backoff delay with jitter."""
        if self.retry_count == 0:
            return 0
        
        # Exponential backoff: base_delay * (2 ^ retry_count)
        delay = self.base_delay * (2 ** min(self.retry_count - 1, 8))  # Cap at 2^8
        delay = min(delay, self.max_delay)
        
        # Add jitter (±25%)
        import random
        jitter = delay * 0.25 * (random.random() - 0.5)
        return max(1.0, delay + jitter)
    
    async def connect_with_retry(self) -> bool:
        """
        Connect to Telegram with exponential backoff retry logic.
        Returns True if connection successful, False otherwise.
        """
        self.retry_count = 0
        
        while self.retry_count < self.max_retries:
            try:
                logger.info(f"🔌 Attempting to connect {self.name} (attempt {self.retry_count + 1}/{self.max_retries})")
                
                # Attempt connection
                if not self.client.is_connected():
                    await asyncio.wait_for(
                        self.client.connect(),
                        timeout=self.connection_timeout
                    )
                
                # Verify connection is actually working
                if not self.client.is_connected():
                    raise ConnectionError("Client reports not connected after connect()")
                
                # Skip connection test during initial connection
                # Health checks will be handled later by the monitoring loop
                logger.info(f"✅ {self.name} basic connection established")
                
                logger.info(f"✅ {self.name} connected successfully")
                self.retry_count = 0
                self.last_error = None
                self._notify_connection_state(True)
                return True
                
            except asyncio.TimeoutError as e:
                self.last_error = f"Connection timeout after {self.connection_timeout}s"
                logger.warning(f"⏰ {self.name} connection timeout: {self.last_error}")
                
            except (OSError, NetworkMigrateError) as e:
                self.last_error = str(e)
                logger.warning(f"🌐 {self.name} network error: {self.last_error}")
                
            except (AuthKeyDuplicatedError, AuthKeyUnregisteredError) as e:
                self.last_error = str(e)
                logger.error(f"🔑 {self.name} auth error: {self.last_error}")
                # Auth errors are usually fatal, don't retry indefinitely
                if self.retry_count > 3:
                    logger.error(f"❌ {self.name} repeated auth errors, giving up")
                    break
                    
            except FloodWaitError as e:
                self.last_error = f"Rate limited for {e.seconds}s"
                logger.warning(f"🚦 {self.name} rate limited: {self.last_error}")
                await asyncio.sleep(e.seconds)
                
            except Exception as e:
                self.last_error = str(e)
                logger.error(f"❌ {self.name} unexpected error: {self.last_error}")
            
            self.retry_count += 1
            
            if self.retry_count < self.max_retries:
                delay = self._calculate_delay()
                logger.info(f"⏳ Retrying {self.name} connection in {delay:.1f}s...")
                await asyncio.sleep(delay)
            else:
                logger.error(f"❌ {self.name} max retries ({self.max_retries}) exceeded")
                
        self._notify_connection_state(False)
        return False
    
    async def disconnect_safely(self):
        """Safely disconnect the client."""
        try:
            if self.client.is_connected():
                logger.info(f"🔌 Disconnecting {self.name}")
                await self.client.disconnect()  # type: ignore
                self._notify_connection_state(False)
                logger.info(f"✅ {self.name} disconnected")
        except Exception as e:
            logger.error(f"Error disconnecting {self.name}: {e}")
    
    async def monitor_connection(self, check_interval: float = 60.0):
        """
        Monitor connection health and automatically reconnect if needed.
        Should be run as a background task.
        """
        self.is_running = True
        logger.info(f"🔍 Starting connection monitor for {self.name} (check every {check_interval}s)")
        
        while self.is_running:
            try:
                await asyncio.sleep(check_interval)
                
                if not self.is_running:
                    break
                
                # Check if client is connected
                if not self.client.is_connected():
                    logger.warning(f"🔌 {self.name} disconnected, attempting reconnection...")
                    success = await self.connect_with_retry()
                    if not success:
                        logger.error(f"❌ Failed to reconnect {self.name}")
                        # Notify about connection failure but continue monitoring
                        continue
                
                # Test connection health with appropriate method for client type
                try:
                    if self.is_bot:
                        # For bot clients, get_me() should always work
                        await asyncio.wait_for(
                            self.client.get_me(),
                            timeout=10.0
                        )
                    else:
                        # For user clients, use a lighter check or skip if not authenticated
                        # We can check if the connection is alive without calling get_me()
                        if not self.client.is_connected():
                            raise ConnectionError("User client disconnected")
                        # If connected, assume it's healthy for now
                        logger.debug(f"📶 {self.name} (user client) connection healthy")
                except Exception as e:
                    if self.is_bot or "not connected" in str(e).lower():
                        logger.warning(f"🏥 {self.name} health check failed: {e}")
                        # Force reconnection
                        try:
                            await self.client.disconnect()  # type: ignore
                        except:
                            pass
                        
                        success = await self.connect_with_retry()
                        if not success:
                            logger.error(f"❌ Failed to reconnect {self.name} after health check failure")
                    else:
                        # For user clients, some API errors are expected if not properly authenticated
                        logger.debug(f"📄 {self.name} (user client) API check skipped: {e}")
                        
            except asyncio.CancelledError:
                logger.info(f"🛑 Connection monitor for {self.name} cancelled")
                break
            except Exception as e:
                logger.error(f"❌ Error in connection monitor for {self.name}: {e}")
                await asyncio.sleep(30)  # Wait before retrying monitor loop
        
        logger.info(f"🔍 Connection monitor for {self.name} stopped")
    
    def stop_monitoring(self):
        """Stop the connection monitoring."""
        self.is_running = False
    
    def get_status(self) -> dict:
        """Get current connection status."""
        return {
            "name": self.name,
            "connected": self.client.is_connected(),
            "retry_count": self.retry_count,
            "last_error": self.last_error,
            "monitoring": self.is_running
        }


class MultiClientManager:
    """
    Manages multiple Telegram clients with coordinated connection handling.
    """
    
    def __init__(self):
        self.managers = {}
        self.status_callbacks = []
        
    def add_client(self, name: str, client: TelegramClient, is_bot: bool = False):
        """Add a client to be managed."""
        manager = ConnectionManager(client, name, is_bot)
        manager.add_connection_callback(self._on_connection_change)
        self.managers[name] = manager
        client_type = "bot" if is_bot else "user"
        logger.info(f"📱 Added {name} ({client_type} client) to connection manager")
    
    def _on_connection_change(self, name: str, connected: bool):
        """Handle connection state changes."""
        status = "connected" if connected else "disconnected"
        logger.info(f"🔄 {name} {status}")
        
        # Notify status callbacks
        for callback in self.status_callbacks:
            try:
                callback(name, connected)
            except Exception as e:
                logger.error(f"Error in status callback: {e}")
    
    def add_status_callback(self, callback: Callable[[str, bool], None]):
        """Add callback for connection status changes."""
        self.status_callbacks.append(callback)
    
    async def connect_all(self) -> dict:
        """Connect all managed clients."""
        logger.info("🚀 Connecting all clients...")
        results = {}
        
        # Connect clients in parallel
        tasks = []
        for name, manager in self.managers.items():
            task = asyncio.create_task(manager.connect_with_retry())
            tasks.append((name, task))
        
        # Wait for all connections
        for name, task in tasks:
            try:
                success = await task
                results[name] = success
                if success:
                    logger.info(f"✅ {name} connected")
                else:
                    logger.error(f"❌ {name} failed to connect")
            except Exception as e:
                logger.error(f"❌ Error connecting {name}: {e}")
                results[name] = False
        
        connected_count = sum(results.values())
        total_count = len(results)
        logger.info(f"🔌 Connected {connected_count}/{total_count} clients")
        
        return results
    
    async def start_monitoring(self, check_interval: float = 60.0):
        """Start monitoring all clients."""
        logger.info(f"🔍 Starting monitoring for all clients (check every {check_interval}s)")
        
        tasks = []
        for name, manager in self.managers.items():
            task = asyncio.create_task(manager.monitor_connection(check_interval))
            tasks.append(task)
        
        return tasks
    
    async def disconnect_all(self):
        """Disconnect all managed clients."""
        logger.info("🔌 Disconnecting all clients...")
        
        # Stop monitoring first
        for manager in self.managers.values():
            manager.stop_monitoring()
        
        # Disconnect clients
        tasks = []
        for name, manager in self.managers.items():
            task = asyncio.create_task(manager.disconnect_safely())
            tasks.append(task)
        
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("✅ All clients disconnected")
    
    def get_status_summary(self) -> dict:
        """Get status summary for all clients."""
        summary = {
            "clients": {},
            "total_connected": 0,
            "total_clients": len(self.managers)
        }
        
        for name, manager in self.managers.items():
            status = manager.get_status()
            summary["clients"][name] = status
            if status["connected"]:
                summary["total_connected"] += 1
        
        return summary


# Global connection manager instance
connection_manager = MultiClientManager()

# Convenience functions for integration
def add_telegram_client(name: str, client: TelegramClient, is_bot: bool = False):
    """Add a Telegram client to the global connection manager."""
    connection_manager.add_client(name, client, is_bot)

async def connect_all_clients() -> dict:
    """Connect all registered Telegram clients."""
    return await connection_manager.connect_all()

async def start_connection_monitoring(check_interval: float = 60.0):
    """Start monitoring all registered clients."""
    return await connection_manager.start_monitoring(check_interval)

async def disconnect_all_clients():
    """Disconnect all registered clients."""
    await connection_manager.disconnect_all()

def get_connection_status() -> dict:
    """Get connection status for all clients."""
    return connection_manager.get_status_summary()

def add_connection_status_callback(callback: Callable[[str, bool], None]):
    """Add a callback for connection status changes."""
    connection_manager.add_status_callback(callback)
