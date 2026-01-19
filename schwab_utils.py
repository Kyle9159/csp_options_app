import os
from schwab import auth
import dotenv
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((ConnectionError, TimeoutError, OSError)),
    before_sleep=lambda retry_state: logger.warning(f"Schwab API call failed, retrying in {retry_state.next_action.sleep} seconds... (attempt {retry_state.attempt_number}/3)")
)
def get_client():
    # Load environment variables
    dotenv.load_dotenv()
    """
    Returns a Schwab API client using easy_client authentication.

    Uses environment variables for configuration:
    - SCHWAB_API_KEY: API key
    - SCHWAB_APP_SECRET: App secret
    - REDIRECT_URI: Redirect URI (default: https://127.0.0.1:8182)

    Token is stored in 'schwab_token.json'
    """
    api_key = os.getenv('SCHWAB_API_KEY')
    app_secret = os.getenv('SCHWAB_APP_SECRET')
    redirect_uri = os.getenv('REDIRECT_URI', 'https://127.0.0.1:8182')
    token_path = 'cache_files/schwab_token.json'

    if not api_key or not app_secret:
        raise ValueError("SCHWAB_API_KEY and SCHWAB_APP_SECRET environment variables must be set")

    client = auth.easy_client(
        api_key=api_key,
        app_secret=app_secret,
        callback_url=redirect_uri,
        token_path=token_path,
        asyncio=False,
        enforce_enums=True
    )

    return client


def sell_put_to_open(account_id, symbol, strike, expiration, contracts, limit_price=None, dry_run=False):
    """
    Place a Sell To Open order for a cash-secured put.

    Args:
        account_id: Schwab account ID
        symbol: Underlying symbol (e.g., 'AAPL')
        strike: Strike price (e.g., 150.0)
        expiration: Expiration date in format 'YYYY-MM-DD'
        contracts: Number of contracts (each = 100 shares)
        limit_price: Limit price per contract (if None, uses market order)
        dry_run: If True, validates but doesn't submit order

    Returns:
        dict: {'success': bool, 'order_id': str, 'message': str}
    """
    try:
        client = get_client()

        # Build option symbol (OCC format: SYMBOL + YYMMDD + C/P + Strike*1000)
        # Example: AAPL  260117P00150000 (AAPL put exp 1/17/26 strike 150)
        from datetime import datetime
        exp_date = datetime.strptime(expiration, '%Y-%m-%d')
        exp_str = exp_date.strftime('%y%m%d')
        strike_str = f"{int(strike * 1000):08d}"
        option_symbol = f"{symbol:<6}{exp_str}P{strike_str}"

        # Build order using schwab-py Order builder
        from schwab.orders.options import option_sell_to_open_limit
        from schwab.orders.common import Duration, Session

        if dry_run:
            return {
                'success': True,
                'order_id': 'DRY_RUN',
                'message': f'DRY RUN: Would sell {contracts}x {symbol} ${strike}P exp {expiration} @ ${limit_price or "MARKET"}'
            }

        # Build order
        if limit_price:
            order = option_sell_to_open_limit(
                option_symbol,
                contracts,
                limit_price
            ).set_duration(Duration.DAY).set_session(Session.NORMAL)
        else:
            # Market orders are risky for options - require limit price
            return {
                'success': False,
                'order_id': None,
                'message': 'Market orders not supported - please specify limit_price'
            }

        # Submit order
        response = client.place_order(account_id, order.build())

        if response.status_code in [200, 201]:
            # Extract order ID from response headers
            order_id = response.headers.get('Location', 'Unknown').split('/')[-1]
            return {
                'success': True,
                'order_id': order_id,
                'message': f'Successfully placed order: Sell {contracts}x {symbol} ${strike}P @ ${limit_price}'
            }
        else:
            return {
                'success': False,
                'order_id': None,
                'message': f'Order failed with status {response.status_code}: {response.text}'
            }

    except Exception as e:
        logger.error(f"Error placing sell to open order: {e}")
        return {
            'success': False,
            'order_id': None,
            'message': f'Error: {str(e)}'
        }


def buy_put_to_close(account_id, symbol, strike, expiration, contracts, limit_price=None, dry_run=False):
    """
    Place a Buy To Close order to close an existing short put position.

    Args:
        account_id: Schwab account ID
        symbol: Underlying symbol (e.g., 'AAPL')
        strike: Strike price (e.g., 150.0)
        expiration: Expiration date in format 'YYYY-MM-DD'
        contracts: Number of contracts to close
        limit_price: Limit price per contract (if None, uses market order)
        dry_run: If True, validates but doesn't submit order

    Returns:
        dict: {'success': bool, 'order_id': str, 'message': str}
    """
    try:
        client = get_client()

        # Build option symbol (OCC format)
        from datetime import datetime
        exp_date = datetime.strptime(expiration, '%Y-%m-%d')
        exp_str = exp_date.strftime('%y%m%d')
        strike_str = f"{int(strike * 1000):08d}"
        option_symbol = f"{symbol:<6}{exp_str}P{strike_str}"

        # Build order using schwab-py Order builder
        from schwab.orders.options import option_buy_to_close_limit
        from schwab.orders.common import Duration, Session

        if dry_run:
            return {
                'success': True,
                'order_id': 'DRY_RUN',
                'message': f'DRY RUN: Would buy to close {contracts}x {symbol} ${strike}P exp {expiration} @ ${limit_price or "MARKET"}'
            }

        # Build order
        if limit_price:
            order = option_buy_to_close_limit(
                option_symbol,
                contracts,
                limit_price
            ).set_duration(Duration.DAY).set_session(Session.NORMAL)
        else:
            # Market orders are risky for options - require limit price
            return {
                'success': False,
                'order_id': None,
                'message': 'Market orders not supported - please specify limit_price'
            }

        # Submit order
        response = client.place_order(account_id, order.build())

        if response.status_code in [200, 201]:
            # Extract order ID from response headers
            order_id = response.headers.get('Location', 'Unknown').split('/')[-1]
            return {
                'success': True,
                'order_id': order_id,
                'message': f'Successfully placed order: Buy to close {contracts}x {symbol} ${strike}P @ ${limit_price}'
            }
        else:
            return {
                'success': False,
                'order_id': None,
                'message': f'Order failed with status {response.status_code}: {response.text}'
            }

    except Exception as e:
        logger.error(f"Error placing buy to close order: {e}")
        return {
            'success': False,
            'order_id': None,
            'message': f'Error: {str(e)}'
        }