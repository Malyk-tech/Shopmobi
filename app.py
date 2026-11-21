
import os, uuid, mimetypes
from dotenv import load_dotenv
load_dotenv()
from functools import wraps
from flask import Flask, request, redirect, url_for, session, flash, render_template_string, abort
from werkzeug.utils import secure_filename
from werkzeug.exceptions import HTTPException
from supabase import create_client

app = Flask(__name__)

@app.get('/health')
def health_check():
    response = app.make_response(('ok', 200))
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return response

app.secret_key = os.environ.get('SHOPMOBI_SECRET') or os.urandom(32).hex()
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024
ALLOWED = {'jpg','jpeg','png','webp'}
SUPABASE_URL = os.environ.get('SUPABASE_URL','').strip()
SUPABASE_KEY = os.environ.get('SUPABASE_KEY','').strip()
SUPABASE_SERVER_KEY = os.environ.get('SUPABASE_SERVICE_ROLE_KEY','').strip()
BUCKET = 'product-image'
if not SUPABASE_URL or not SUPABASE_KEY or not SUPABASE_SERVER_KEY:
    raise RuntimeError('Missing SUPABASE_URL, SUPABASE_KEY, or SUPABASE_SERVICE_ROLE_KEY environment variable.')
# Server-only database client. Never expose this key to browser code.
db = create_client(SUPABASE_URL, SUPABASE_SERVER_KEY)

def rows(table, **filters):
    q = db.table(table).select('*')
    for k,v in filters.items(): q = q.eq(k,v)
    return q.execute().data or []

def one(table, **filters):
    found = rows(table, **filters)
    return found[0] if found else None

def insert(table, data):
    return db.table(table).insert(data).execute().data or []
def auth_client():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def delete(table, **filters):
    q = db.table(table).delete()
    for k, v in filters.items():
        q = q.eq(k, v)
    return q.execute().data or []


def update(table, data, **filters):
    q = db.table(table).update(data)
    for k, v in filters.items():
        q = q.eq(k, v)
    return q.execute().data or []


def public_product(p):
    merchant = one('merchants', id=p.get('merchant_id')) if p.get('merchant_id') else None

    return {
        **p,
        'image': p.get('image_url'),
        'seller_name': merchant.get('business_name', 'Shopmobi seller') if merchant else 'Shopmobi seller',
        'price': float(p.get('price') or 0),
        'stock': int(p.get('stock') or 0)
    }

def all_products(active_only=False):
    q = db.table('products').select('*').order('created_at', desc=True)
    if active_only:
        q = q.eq('is_active', True)
    return [public_product(p) for p in (q.execute().data or [])]

def current_user():
    token=session.get('access_token')
    if not token: return None
    try:
        result=db.auth.get_user(token)
        return result.user
    except Exception:
        session.pop('access_token',None); session.pop('auth_user_id',None)
        return None

def current_merchant():
    user=current_user()
    if not user: return None
    return one('merchants',user_id=user.id)

def current_customer():
    user = current_user()
    if not user:
        return None
    profile = one('profiles', id=str(user.id))
    return profile if profile and profile.get('role') == 'customer' else None


def cart_rows():
    """Build the shopping cart from the visitor's signed Flask session."""
    guest_cart = session.get('guest_cart', {})
    if not isinstance(guest_cart, dict):
        guest_cart = {}
    result = []
    cleaned = {}
    for product_id, quantity in guest_cart.items():
        product = one('products', id=product_id)
        if not product or not product.get('is_active') or int(product.get('stock') or 0) < 1:
            continue
        try:
            quantity = max(1, min(int(quantity), int(product.get('stock') or 0)))
        except (TypeError, ValueError):
            continue
        cleaned[str(product_id)] = quantity
        result.append({'item': {'id': str(product_id), 'product_id': str(product_id)},
                       'product': public_product(product), 'quantity': quantity})
    if cleaned != guest_cart:
        session['guest_cart'] = cleaned
        session.modified = True
    return result


def render_page(title, template, **context):
    customer = current_customer()
    count = sum(x['quantity'] for x in cart_rows())
    inner = render_template_string(template, **context)
    return render_template_string(BASE, title=title, content=inner,
                                  cart_count=count, customer=customer)

def seller_approved(view):
    @wraps(view)
    def wrapped(*args,**kwargs):
        m=current_merchant()
        if not m:
            flash('Please sign in to your seller account first.'); return redirect(url_for('merchant_login'))
        if m.get('status')!='approved':
            flash('Your seller account is '+str(m.get('status','pending'))+'. Please wait for admin approval.'); return redirect(url_for('merchant_login'))
        return view(*args,**kwargs)
    return wrapped

def admin_required(view):
    @wraps(view)
    def wrapped(*args,**kwargs):
        if not session.get('is_admin'): return redirect(url_for('admin_login'))
        return view(*args,**kwargs)
    return wrapped

def allowed_image(filename): return '.' in filename and filename.rsplit('.',1)[1].lower() in ALLOWED


def upload_image(file, merchant_id):
    if not file or not file.filename:
        raise ValueError('No image selected.')

    ext = file.filename.rsplit('.', 1)[-1].lower()
    if ext not in ALLOWED:
        raise ValueError('Unsupported image format.')

    name = f'{merchant_id}/{uuid.uuid4().hex}.{ext}'
    content = file.read()

    if not content:
        raise ValueError('The selected image is empty.')

    mime = file.mimetype or mimetypes.guess_type(file.filename)[0] or 'application/octet-stream'

    result = db.storage.from_(BUCKET).upload(
        name,
        content,
        {'content-type': mime, 'upsert': 'true'}
    )

    return db.storage.from_(BUCKET).get_public_url(name)
@app.route('/')
def home():
    query=request.args.get('q','').strip().lower(); category=request.args.get('category','').strip()
    visible=[p for p in all_products(True) if p['stock']>0 and (not query or query in p['name'].lower() or query in (p.get('description') or '').lower()) and (not category or p['category']==category)]
    customer = current_customer()
    favorite_ids = set()
    content=r'''<section class="hero"><div><small>✦ Thoughtfully picked finds</small><h1>Make space for something better.</h1><p>Discover pieces you’ll love, from everyday essentials to little upgrades that make life feel special.</p><a class="btn" href="#catalog">Explore the catalog ↗</a></div><div class="hero-art"><div class="hero-placeholder">🛍️</div></div></section><div class="section-head"><div><h2 id="catalog">The catalog</h2><p>Find your next favourite thing.</p></div><span class="muted small">{{ visible|length }} products</span></div><div class="chips"><a class="chip" href="{{ url_for('home') }}">All items</a>{% for c in ['Electronics','Fashion','Beauty','Home & Living','Home','Food','Books','Sports','Other'] %}<a class="chip" href="{{ url_for('home',category=c,q=query) }}">{{ c }}</a>{% endfor %}</div>{% if visible %}<div class="grid">{% for p in visible %}<article class="product-card"><a href="{{ url_for('product_detail',product_id=p.id) }}"><div class="product-photo">{% if p.image %}<img src="{{ p.image }}" alt="{{ p.name }}">{% else %}<div class="no-photo">✦</div>{% endif %}</div></a><div class="product-info"><div class="product-meta">{{ p.category }} · {{ p.seller_name }}</div><a href="{{ url_for('product_detail',product_id=p.id) }}"><h3>{{ p.name }}</h3></a><div class="price-row"><div><div class="price">KSh {{ '%.2f'|format(p.price) }}</div><div class="stock">{{ p.stock }} available</div></div><form method="post" action="{{ url_for('add_to_cart',product_id=p.id) }}"><button class="mini-add">+</button></form></div></div></article>{% endfor %}</div>{% else %}<div class="panel empty"><h3>No products found yet</h3><p>Try another search, or add products through an approved seller account.</p></div>{% endif %}'''
    return render_page('Discover',content,visible=visible,query=query,favorite_ids=favorite_ids)

@app.route('/product/<product_id>')
def product_detail(product_id):
    raw=one('products',id=product_id)
    if not raw or not raw.get('is_active'): flash('Product not found.'); return redirect(url_for('home'))
    p=public_product(raw)
    content=r'''<div class="section-head"><a class="muted" href="{{ url_for('home') }}">← Back to catalog</a></div><section class="detail-grid"><div class="detail-photo">{% if p.image %}<img src="{{ p.image }}" alt="{{ p.name }}">{% else %}✦{% endif %}</div><div><div class="product-meta">{{ p.category }} · Sold by {{ p.seller_name }}</div><h1 class="detail-title">{{ p.name }}</h1><div class="price">KSh {{ '%.2f'|format(p.price) }}</div><p class="muted">{{ p.description }}</p><p class="stock">{{ p.stock }} available</p><form class="product-buy-controls" method="post" action="{{ url_for('add_to_cart',product_id=p.id) }}"><input type="number" name="quantity" value="1" min="1" max="{{ p.stock }}"><button class="btn-green" type="submit">Add to cart</button></form><p class="muted small">Demo checkout only. No real payments are collected.</p></div></section>'''
    return render_page(p['name'],content,p=p)

@app.route('/cart')
def cart():
    items = cart_rows()
    total = sum(x['product']['price'] * x['quantity'] for x in items)
    content = r'''<div class="section-head"><h2>Your bag</h2></div>{% if items %}<div class="two-col"><div class="panel">{% for x in items %}<div class="cart-item"><div class="cart-item-photo">{% if x.product.image %}<img src="{{ x.product.image }}">{% else %}✦{% endif %}</div><div class="cart-item-info"><strong class="cart-item-name">{{ x.product.name }}</strong><p>KSh {{ '%.2f'|format(x.product.price) }}</p><div class="cart-controls"><form method="post" action="{{ url_for('update_cart_quantity',item_id=x.item.id) }}"><input type="hidden" name="quantity" value="{{ x.quantity - 1 }}"><button class="cart-qty-btn" type="submit" aria-label="Decrease quantity" {% if x.quantity <= 1 %}disabled{% endif %}>−</button></form><span class="cart-qty-value">{{ x.quantity }}</span><form method="post" action="{{ url_for('update_cart_quantity',item_id=x.item.id) }}"><input type="hidden" name="quantity" value="{{ x.quantity + 1 }}"><button class="cart-qty-btn" type="submit" aria-label="Increase quantity" {% if x.quantity >= x.product.stock %}disabled{% endif %}>+</button></form><form class="cart-remove-form" method="post" action="{{ url_for('remove_cart_item',item_id=x.item.id) }}"><button class="cart-remove-btn" type="submit" title="Remove item" aria-label="Remove item">×</button></form></div><strong class="cart-item-subtotal">Subtotal: KSh {{ '%.2f'|format(x.product.price * x.quantity) }}</strong></div></div>{% endfor %}</div><div class="panel"><h3>Order summary</h3><strong class="cart-summary-total">Total: KSh {{ '%.2f'|format(total) }}</strong><form method="post" action="{{ url_for('checkout') }}"><div class="field"><label>Full name</label><input name="name" autocomplete="name" required></div><div class="field"><label>Phone number</label><input name="phone" type="tel" autocomplete="tel" required></div><div class="field"><label>Delivery address</label><textarea name="address" autocomplete="street-address" required></textarea></div><button class="btn-green">Place demo order</button></form><p class="muted small">No payment will be taken.</p></div></div>{% else %}<div class="panel">Your bag is empty. <a href="{{ url_for('home') }}">Explore catalog</a></div>{% endif %}'''
    return render_page('Your bag', content, items=items, total=total)


@app.route('/cart/remove/<item_id>', methods=['POST'])
def remove_cart_item(item_id):
    guest_cart = session.get('guest_cart', {})
    guest_cart.pop(str(item_id), None)
    session['guest_cart'] = guest_cart
    flash('Item removed from your bag.')
    return redirect(url_for('cart'))


@app.route('/cart/update/<item_id>', methods=['POST'])
def update_cart_quantity(item_id):
    guest_cart = session.get('guest_cart', {})
    product = one('products', id=item_id)
    try:
        quantity = int(request.form.get('quantity', '1'))
    except (TypeError, ValueError):
        quantity = 0
    if not product or not product.get('is_active') or quantity < 1 or quantity > int(product.get('stock') or 0):
        flash('Please enter a valid quantity within available stock.')
    else:
        guest_cart[str(item_id)] = quantity
        session['guest_cart'] = guest_cart
        flash('Cart updated.')
    return redirect(url_for('cart'))


@app.route('/cart/add/<product_id>', methods=['POST'])
def add_to_cart(product_id):
    product = one('products', id=product_id)
    if not product or not product.get('is_active') or int(product.get('stock') or 0) < 1:
        flash('This product is unavailable.')
        return redirect(request.referrer or url_for('home'))
    try:
        quantity = int(request.form.get('quantity', '1'))
    except (TypeError, ValueError):
        quantity = 0
    if quantity < 1:
        flash('Choose a valid quantity.')
    else:
        guest_cart = session.get('guest_cart', {})
        if not isinstance(guest_cart, dict):
            guest_cart = {}
        new_quantity = int(guest_cart.get(str(product_id), 0)) + quantity
        if new_quantity > int(product.get('stock') or 0):
            flash('Not enough stock available.')
        else:
            guest_cart[str(product_id)] = new_quantity
            session['guest_cart'] = guest_cart
            flash('Added to your bag.')
    return redirect(request.referrer or url_for('home'))


@app.route('/checkout', methods=['POST'])
def checkout():
    items = cart_rows()
    if not items:
        flash('Your bag is empty.')
        return redirect(url_for('cart'))
    name = request.form.get('name', '').strip()
    phone = request.form.get('phone', '').strip()
    address = request.form.get('address', '').strip()
    if not all([name, phone, address]):
        flash('Complete all delivery details.')
        return redirect(url_for('cart'))

    total = sum(x['product']['price'] * x['quantity'] for x in items)
    for item in items:
        fresh = one('products', id=item['product']['id'])
        if not fresh or not fresh.get('is_active') or int(fresh.get('stock') or 0) < item['quantity']:
            flash('Stock changed. Review your cart.')
            return redirect(url_for('cart'))

    try:
        # Guest orders require orders.customer_id to allow NULL.
        created = insert('orders', {
            'customer_id': None,
            'total_amount': total,
            'status': 'New',
            'payment_status': 'pending',
            'delivery_address': f'{name} | {phone} | {address}'
        })
        if not created:
            raise RuntimeError('The order was not created.')
        order = created[0]
        for item in items:
            product_id = item['product']['id']
            fresh = one('products', id=product_id)
            if not fresh or int(fresh.get('stock') or 0) < item['quantity']:
                raise RuntimeError('Stock changed while placing the order. Please try again.')
            insert('order_items', {
                'order_id': order['id'],
                'product_id': product_id,
                'quantity': item['quantity'],
                'unit_price': item['product']['price']
            })
            update('products', {'stock': int(fresh['stock']) - item['quantity']}, id=product_id)
        session.pop('guest_cart', None)
        flash('Demo order placed! No payment was collected.')
        return redirect(url_for('home'))
    except Exception:
        app.logger.exception('SHOPMOBI GUEST CHECKOUT FAILED')
        flash('We could not place your order. Please try again or contact the shop.')
        return redirect(url_for('cart'))


@app.route('/auth/callback')
def auth_callback():
    code = request.args.get('code')

    if not code:
        flash('Email verification link is invalid or expired. Please request a new one.')
        return redirect(url_for('merchant_login'))

    try:
        result = auth_client().auth.exchange_code_for_session(code)

        if not result.session:
            raise ValueError('Supabase did not return a session.')


        session['auth_user_id'] = result.user.id
        session['access_token'] = result.session.access_token

        flash('Email verified successfully. You can now sign in.')
        return redirect(url_for('merchant_login'))

    except Exception:
        app.logger.exception('SHOPMOBI EMAIL VERIFICATION FAILED')
        flash('Email verification failed or the link has expired. Please request a new email.')
        return redirect(url_for('merchant_login'))

@app.route('/seller/register',methods=['GET','POST'])
def merchant_register():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        if not name or not email or len(password) < 8:
            flash('Enter a business name, valid email and password of at least 8 characters.')
        else:
            try:
                # If a Shopmobi profile already exists, direct the user to sign in.
                existing_profile = one('profiles', email=email)
                if existing_profile:
                    flash('This account already exists. Please sign in instead.')
                    return redirect(url_for('merchant_login'))

                # Create the Supabase Auth account.
                result = auth_client().auth.sign_up({
    'email': email,
    'password': password,
    'options': {
        'email_redirect_to': url_for('auth_callback', _external=True)
    }
})
                user = result.user
                if not user:
                    raise ValueError(
                        'Supabase did not return a user. Check email confirmation settings '
                        'or whether this email is already registered.'
                    )

                # Create the profile and pending seller application.
                insert('profiles', {
                    'id': str(user.id),
                    'full_name': name,
                    'email': email,
                    'role': 'merchant'
                })
                insert('merchants', {
                    'user_id': str(user.id),
                    'business_name': name,
                    'status': 'pending'
                })

                flash('Application submitted. Check your email if confirmation is required, then sign in after admin approval.')
                return redirect(url_for('merchant_login'))

            except Exception as e:
                # Keep the full diagnostic in the server log, not in the public page.
                app.logger.exception('SHOPMOBI MERCHANT REGISTRATION FAILED')
                message = str(e).lower()
                if any(term in message for term in (
                    'duplicate key', 'already registered', 'user already exists',
                    'already been registered', 'email_exists'
                )):
                    flash('This account already exists. Please sign in instead.')
                    return redirect(url_for('merchant_login'))
                flash('Registration failed. Please try again. If it continues, check the server log for the exact error.')

    content = r'''<div class="section-head"><h2>Become a seller</h2></div>
    <div class="panel">
    <form method="post">
    <div class="field"><label>Business or seller name</label><input name="name" required></div>
    <div class="field"><label>Email address</label><input type="email" name="email" required></div>
    <div class="field"><label>Password (minimum 8 characters)</label><input type="password" name="password" minlength="8" required></div>
    <button class="btn-green" type="submit">Submit application</button>
    </form>
    <p>Already have an account? <a href="{{ url_for('merchant_login') }}">Sign in here</a></p>
    </div>'''
    return render_page('Seller application', content)

@app.route('/seller/login',methods=['GET','POST'])
def merchant_login():
    if request.method=='POST':
        email=request.form.get('email','').strip().lower(); password=request.form.get('password','')
        try:
            result=auth_client().auth.sign_in_with_password({'email':email,'password':password})
            user=result.user; token=result.session.access_token if result.session else None
            if not user or not token: raise ValueError('Sign-in did not return an active session.')
            merchant=one('merchants',user_id=user.id)
            if not merchant: raise ValueError('No seller application is linked to this account.')
            session['auth_user_id']=user.id; session['access_token']=token
            if merchant['status']!='approved':
                flash('Your seller account is '+merchant['status']+'. Please wait for admin approval.')
            else: return redirect(url_for('merchant_dashboard'))
        except Exception: flash('Incorrect email or password, or email confirmation is still required.')
    content=r'''<div class="section-head"><h2>Seller sign in</h2></div><div class="panel"><form method="post"><div class="field"><label>Email address</label><input type="email" name="email" required></div><div class="field"><label>Password</label><input type="password" name="password" required></div><button class="btn-green">Sign in</button></form><p><a href="{{ url_for('forgot_password') }}">Forgot password?</a></p><p>New to Shopmobi? <a href="{{ url_for('merchant_register') }}">Apply to sell</a></p></div>'''
    return render_page('Seller sign in',content)

@app.route('/seller/forgot-password',methods=['GET','POST'])
def forgot_password():
    if request.method=='POST':
        email=request.form.get('email','').strip().lower()
        try: auth_client().auth.reset_password_for_email(email,{'redirect_to':url_for('password_reset',_external=True)}); flash('If the account exists, a password reset email has been sent.')
        except Exception: flash('Could not send the reset email. Check the email address and try again.')
    return render_page('Reset password',r'''<div class="panel"><h2>Forgot password</h2><form method="post"><div class="field"><label>Email address</label><input type="email" name="email" required></div><button class="btn-green">Send reset link</button></form></div>''')

@app.route('/seller/password-reset',methods=['GET','POST'])
def password_reset():
    token=request.args.get('access_token') or request.form.get('access_token') or session.get('recovery_token')
    if token: session['recovery_token']=token
    if request.method=='POST':
        password=request.form.get('password','')
        if len(password)<8: flash('Use at least 8 characters.')
        else:
            try:
                client=auth_client(); access=request.form.get('access_token') or session.get('recovery_token'); refresh=request.form.get('refresh_token')
                if not access or not refresh: raise ValueError('Recovery session is missing or expired.')
                client.auth.set_session(access,refresh)
                client.auth.update_user({'password':password}); session.pop('recovery_token',None); flash('Password updated. Please sign in.'); return redirect(url_for('merchant_login'))
            except Exception as e: flash('Password reset link may have expired. Request a new one.')
    return render_page('Choose a new password',r'''<div class="panel"><h2>Choose a new password</h2><form method="post" id="resetForm"><input type="hidden" name="access_token" id="accessToken"><input type="hidden" name="refresh_token" id="refreshToken"><div class="field"><label>New password</label><input type="password" name="password" minlength="8" required></div><button class="btn-green">Update password</button></form><p id="resetHelp" class="muted small"></p></div><script>(function(){const h=new URLSearchParams(location.hash.slice(1));document.getElementById('accessToken').value=h.get('access_token')||'';document.getElementById('refreshToken').value=h.get('refresh_token')||'';if(!h.get('access_token'))document.getElementById('resetHelp').textContent='Open the latest password-reset link from your email.';})();</script>''')

@app.route('/seller/logout')
def merchant_logout():
    session.pop('access_token',None); session.pop('auth_user_id',None); flash('You have signed out.'); return redirect(url_for('home'))

@app.route('/seller/dashboard')
@seller_approved
def merchant_dashboard():
    m=current_merchant(); own=[public_product(p) for p in rows('products',merchant_id=m['id']) if p.get('is_active', True)]
    content=r'''<div class="section-head"><div><h2>Seller studio</h2><p>Welcome back, {{ m.business_name }}.</p></div><a class="btn btn-green" href="{{ url_for('product_add') }}">＋ Add product</a></div><div class="metric-grid"><div class="metric"><small>Your products</small><strong>{{ own|length }}</strong></div><div class="metric"><small>Units in stock</small><strong>{{ own|sum(attribute='stock') }}</strong></div></div>{% if own %}<div class="panel table-wrap"><table><tr><th>Photo</th><th>Product</th><th>Price</th><th>Stock</th><th>Actions</th></tr>{% for p in own %}<tr><td>{% if p.image %}<img class="table-product-image" src="{{ p.image }}">{% endif %}</td><td>{{ p.name }}</td><td>KSh {{ '%.2f'|format(p.price) }}</td><td>{{ p.stock }}</td><td><a class="btn btn-light" href="{{ url_for('product_edit',product_id=p.id) }}">Edit</a><form method="post" action="{{ url_for('product_delete',product_id=p.id) }}"><button class="btn-danger">Delete</button></form></td></tr>{% endfor %}</table></div>{% else %}<div class="panel">No products yet.</div>{% endif %}<a class="btn btn-light" href="{{ url_for('merchant_logout') }}">Sign out</a>'''
    return render_page('Seller studio',content,m=m,own=own)


@app.route('/seller/products/add', methods=['GET', 'POST'])
@seller_approved
def product_add():
    m = current_merchant()

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        desc = request.form.get('description', '').strip()
        category = request.form.get('category', 'Other').strip()
        image = request.files.get('image')

        try:
            price = float(request.form.get('price', '0'))
            stock = int(request.form.get('stock', '0'))
        except ValueError:
            price, stock = -1, -1

        if not name or price <= 0 or stock < 0:
            flash('Enter a product name, positive price and valid stock quantity.')
        elif not image or not image.filename or not allowed_image(image.filename):
            flash('Choose a JPG, PNG or WEBP product photo.')
        else:
            try:
                image_url = upload_image(image, m['id'])
                insert('products', {
                    'merchant_id': m['id'],
                    'name': name,
                    'description': desc,
                    'category': category,
                    'price': price,
                    'stock': stock,
                    'image_url': image_url,
                    'is_active': True
                })
                flash('Product and photo added successfully.')
                return redirect(url_for('merchant_dashboard'))
            except Exception as e:
                flash('Product could not be saved: ' + str(e)[:180])

    content = r'''<div class="section-head"><h2>Add a product</h2></div><div class="panel"><form method="post" enctype="multipart/form-data"><div class="field"><label>Product name</label><input name="name" required></div><div class="field"><label>Category</label><select name="category">{% for c in ['Electronics','Fashion','Beauty','Home & Living','Home','Food','Books','Sports','Other'] %}<option>{{ c }}</option>{% endfor %}</select></div><div class="field"><label>Price (KSh)</label><input type="number" name="price" min="0.01" step="0.01" required></div><div class="field"><label>Stock quantity</label><input type="number" name="stock" min="0" required></div><div class="field"><label>Description</label><textarea name="description"></textarea></div><div class="field"><label>Product photo (max 5 MB)</label><input type="file" name="image" accept="image/jpeg,image/png,image/webp" required></div><button class="btn-green">Save product</button></form></div>'''

    return render_page('Add product', content)

@app.route('/seller/products/<product_id>/edit',methods=['GET','POST'])
@seller_approved
def product_edit(product_id):
    m=current_merchant(); raw=one('products',id=product_id,merchant_id=m['id'])
    if not raw: flash('Product not found.'); return redirect(url_for('merchant_dashboard'))
    p=public_product(raw)
    if request.method=='POST':
        image=request.files.get('image')
        try: price=float(request.form.get('price','0')); stock=int(request.form.get('stock','0'))
        except ValueError: price=-1; stock=-1
        if price<=0 or stock<0: flash('Enter a positive price and valid stock quantity.')
        elif image and image.filename and not allowed_image(image.filename): flash('Use JPG, PNG or WEBP.')
        else:
            data={'name':request.form.get('name','').strip(),'description':request.form.get('description','').strip(),'category':request.form.get('category','Other').strip(),'price':price,'stock':stock}
            try:
                if image and image.filename: data['image_url']=upload_image(image,m['id'])
                update('products',data,id=product_id,merchant_id=m['id']); flash('Product updated.'); return redirect(url_for('merchant_dashboard'))
            except Exception as e: flash('Could not update product: '+str(e)[:160])
    content=r'''<div class="panel"><h2>Edit product</h2><form method="post" enctype="multipart/form-data"><div class="field"><label>Name</label><input name="name" value="{{ p.name }}" required></div><div class="field"><label>Category</label><input name="category" value="{{ p.category }}"></div><div class="field"><label>Description</label><textarea name="description">{{ p.description }}</textarea></div><div class="field"><label>Price</label><input name="price" type="number" step="0.01" value="{{ p.price }}"></div><div class="field"><label>Stock</label><input name="stock" type="number" value="{{ p.stock }}"></div>{% if p.image %}<img class="table-product-image" style="width:140px;height:140px" src="{{ p.image }}">{% endif %}<div class="field"><label>Replace photo</label><input type="file" name="image" accept="image/jpeg,image/png,image/webp"></div><button class="btn-green">Save changes</button></form></div>'''
    return render_page('Edit product',content,p=p)

@app.route('/seller/products/<product_id>/delete',methods=['POST'])
@seller_approved
def product_delete(product_id):
    m=current_merchant(); p=one('products',id=product_id,merchant_id=m['id'])
    if p: update('products',{'is_active':False},id=product_id,merchant_id=m['id']); flash('Product removed from the marketplace.')
    else: flash('Product not found.')
    return redirect(url_for('merchant_dashboard'))

ADMIN_USERNAME=os.environ.get('SHOPMOBI_ADMIN','admin')
ADMIN_PASSWORD=os.environ.get('SHOPMOBI_ADMIN_PASSWORD','')
from werkzeug.security import check_password_hash, generate_password_hash
ADMIN_HASH=generate_password_hash(ADMIN_PASSWORD) if ADMIN_PASSWORD else None
@app.route('/admin/login',methods=['GET','POST'])
def admin_login():
    if request.method=='POST':
        password=request.form.get('password','')
        if request.form.get('username','')==ADMIN_USERNAME and ADMIN_HASH and check_password_hash(ADMIN_HASH,password): session['is_admin']=True; return redirect(url_for('admin_dashboard'))
        flash('Incorrect admin username or password.')
    return render_page('Admin sign in',r'''<div class="panel"><h2>Admin sign in</h2><form method="post"><div class="field"><label>Username</label><input name="username" required></div><div class="field"><label>Password</label><input name="password" type="password" required></div><button class="btn-green">Sign in</button></form></div>''')
@app.route('/admin/logout')
def admin_logout(): session.pop('is_admin',None); return redirect(url_for('home'))
@app.route('/admin')
@admin_required
def admin_dashboard():
    merchants=rows('merchants'); profiles={p['id']:p for p in rows('profiles')}; products=all_products(); orders=rows('orders')
    for m in merchants: m['name']=m['business_name']; m['email']=profiles.get(m['user_id'],{}).get('email','')
    content=r'''<div class="section-head"><h2>Marketplace overview</h2><a class="btn btn-light" href="{{ url_for('admin_logout') }}">Sign out</a></div><div class="metric-grid"><div class="metric"><small>Total sellers</small><strong>{{ merchants|length }}</strong></div><div class="metric"><small>Pending approvals</small><strong>{{ pending }}</strong></div><div class="metric"><small>Total products</small><strong>{{ products|length }}</strong></div><div class="metric"><small>Orders</small><strong>{{ orders|length }}</strong></div></div><div class="section-head"><h2>Seller applications</h2></div>{% for m in merchants %}<div class="panel"><strong>{{ m.name }}</strong><p class="muted">{{ m.email }}</p><span class="status">{{ m.status }}</span>{% for d in ['approve','reject','suspend'] %}{% if (d=='approve' and m.status!='approved') or (d=='reject' and m.status!='rejected') or (d=='suspend' and m.status!='suspended') %}<form style="display:inline" method="post" action="{{ url_for('merchant_decision',merchant_id=m.id,decision=d) }}"><button class="{% if d=='approve' %}btn-green{% else %}btn-danger{% endif %}">{{ d|title }}</button></form>{% endif %}{% endfor %}</div>{% endfor %}<div class="section-head"><h2>All products</h2></div>{% for p in products %}<div class="panel">{{ p.name }} · {{ p.seller_name }} · KSh {{ '%.2f'|format(p.price) }} · Stock {{ p.stock }}</div>{% else %}<div class="panel">No products yet.</div>{% endfor %}'''
    return render_page('Admin dashboard',content,merchants=merchants,pending=sum(m['status']=='pending' for m in merchants),products=products,orders=orders)
@app.route('/admin/merchants/<merchant_id>/<decision>',methods=['POST'])
@admin_required
def merchant_decision(merchant_id,decision):
    statuses={'approve':'approved','reject':'rejected','suspend':'suspended'}
    if decision not in statuses or not one('merchants',id=merchant_id): flash('Invalid seller or action.')
    else: update('merchants',{'status':statuses[decision]},id=merchant_id); flash('Seller account '+statuses[decision]+'.')
    return redirect(url_for('admin_dashboard'))

@app.errorhandler(413)
def too_large(e): return 'Image is too large. Please upload an image smaller than 5 MB.',413

@app.errorhandler(Exception)
def handle_unexpected(e):
    if isinstance(e, HTTPException):
        return e
    app.logger.exception('Shopmobi request failed')
    return 'Something went wrong. Please try again. If the issue continues, contact the administrator.',500

CSS = '\n:root{--bg:#f1f2ec;--surface:#fff;--surface2:#e7e9df;--text:#172018;--muted:#687267;--line:#dce1d7;--green:#173d2b;--green2:#2d6444;--accent:#a8c66c;--black:#080b09;--shadow:0 12px 35px rgba(18,40,24,.08)}\nhtml[data-theme="dark"]{--bg:#111a14;--surface:#1b2820;--surface2:#26362a;--text:#f0f5ed;--muted:#b1beb0;--line:#344638;--green:#c0df9a;--green2:#a8d081;--accent:#b7d98a;--black:#070b08;--shadow:0 12px 35px rgba(0,0,0,.22)}\n*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;transition:background .2s,color .2s}a{color:inherit;text-decoration:none}button,input,select,textarea{font:inherit}button,.btn{border:0;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:11px 16px;border-radius:999px;background:var(--black);color:white;font-weight:650;text-decoration:none;transition:transform .15s,opacity .15s}button:hover,.btn:hover{opacity:.88;transform:translateY(-1px)}.btn-light{background:var(--surface2);color:var(--text)}.btn-green{background:var(--green);color:#fff}.btn-danger{background:#a82c2c;color:#fff}.icon-btn{width:42px;height:42px;padding:0;background:var(--surface);color:var(--text);border:1px solid var(--line);font-size:18px}.wrap{width:min(1180px,92%);margin:auto}.topbar{position:sticky;top:0;z-index:10;background:color-mix(in srgb,var(--bg) 90%,transparent);backdrop-filter:blur(16px);border-bottom:1px solid var(--line)}.nav{min-height:76px;display:flex;align-items:center;justify-content:space-between;gap:20px}.logo{font-size:25px;font-weight:900;letter-spacing:-1.3px;color:var(--green)}.logo span{color:var(--text)}.navlinks{display:flex;align-items:center;gap:20px;color:var(--muted);font-size:14px}.navlinks a:hover{color:var(--text)}.navactions{display:flex;gap:8px;align-items:center}.search{display:flex;align-items:center;gap:9px;background:var(--surface);border:1px solid var(--line);border-radius:999px;padding:0 15px;min-width:210px}.search input{border:0;outline:0;background:transparent;color:var(--text);padding:12px 0;width:100%}.hero{margin:30px auto 36px;border-radius:30px;overflow:hidden;min-height:350px;background:radial-gradient(circle at 85% 10%,#678b51 0,transparent 34%),linear-gradient(115deg,#102c1e,#315b38 58%,#8eaa6b);color:#fff;display:grid;grid-template-columns:1.1fr .9fr;align-items:center;padding:48px clamp(25px,5vw,70px);position:relative}.hero small{display:inline-block;background:#ffffff1f;border:1px solid #ffffff30;padding:8px 13px;border-radius:999px;letter-spacing:.08em;text-transform:uppercase;font-size:11px}.hero h1{font-size:clamp(38px,5vw,65px);line-height:.99;letter-spacing:-3px;max-width:560px;margin:22px 0 15px}.hero p{max-width:440px;color:#e2eadd;line-height:1.65}.hero .btn{background:#f5f6ee;color:#15271a;margin-top:10px}.hero-art{display:flex;justify-content:center;align-items:center;min-height:240px}.hero-art img{width:min(100%,400px);height:280px;object-fit:contain;filter:drop-shadow(0 25px 20px #06150b60)}.hero-placeholder{font-size:140px;filter:drop-shadow(0 20px 12px #06150b55)}.section-head{display:flex;justify-content:space-between;align-items:end;gap:14px;margin:32px 0 18px}.section-head h2{font-size:clamp(24px,3vw,32px);letter-spacing:-1px;margin:0}.section-head p{margin:7px 0 0;color:var(--muted)}.chips{display:flex;gap:9px;overflow-x:auto;padding:2px 0 8px;margin-bottom:15px}.chip{white-space:nowrap;border:1px solid var(--line);border-radius:999px;padding:10px 16px;background:var(--surface);color:var(--text);font-size:13px}.chip.active{background:var(--black);color:#fff;border-color:var(--black)}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:18px}.product-card{background:var(--surface);border:1px solid var(--line);border-radius:22px;padding:12px;min-width:0;box-shadow:var(--shadow);position:relative;display:flex;flex-direction:column}.product-photo{height:205px;background:var(--surface2);border-radius:15px;overflow:hidden;display:flex;align-items:center;justify-content:center}.product-photo img{width:100%;height:100%;object-fit:cover}.no-photo{font-size:60px}.heart{position:absolute;top:21px;right:21px;width:36px;height:36px;border-radius:50%;background:var(--surface);color:var(--text);padding:0;border:1px solid var(--line)}.heart.on{color:#bc303d}.product-info{padding:13px 4px 4px;display:flex;flex-direction:column;gap:7px;flex:1}.product-info h3{margin:0;font-size:16px;letter-spacing:-.3px}.muted{color:var(--muted)}.product-meta{font-size:12px;color:var(--muted)}.price-row{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:auto;padding-top:7px}.price{font-weight:850;font-size:19px;letter-spacing:-.4px}.mini-add{width:36px;height:36px;padding:0;border-radius:50%;font-size:20px}.stock{font-size:12px;color:var(--muted)}.panel{background:var(--surface);border:1px solid var(--line);border-radius:22px;padding:22px;margin:18px 0;box-shadow:var(--shadow)}.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 16px}.field{margin-bottom:13px}.field.full{grid-column:1/-1}.field label{display:block;font-size:13px;font-weight:700;margin-bottom:7px}.field input,.field select,.field textarea{width:100%;border:1px solid var(--line);border-radius:12px;background:var(--bg);color:var(--text);padding:12px 13px;outline-color:var(--green2)}.field textarea{min-height:90px;resize:vertical}.notice{background:var(--surface2);border:1px solid var(--line);padding:13px 16px;border-radius:13px;margin:13px 0}.table-wrap{overflow-x:auto}.table-product-image{width:68px;height:68px;object-fit:cover;border-radius:12px;background:var(--surface2);display:block}.table-product-placeholder{width:68px;height:68px;border-radius:12px;background:var(--surface2);display:grid;place-items:center;font-size:25px}table{width:100%;border-collapse:collapse;min-width:650px}th,td{text-align:left;padding:13px 11px;border-bottom:1px solid var(--line);font-size:14px}th{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}.status{display:inline-block;border-radius:999px;padding:5px 9px;font-size:12px;background:var(--surface2)}.footer{margin-top:60px;padding:30px 0;border-top:1px solid var(--line);color:var(--muted);font-size:13px}.two-col{display:grid;grid-template-columns:1fr 1fr;gap:20px}.empty{text-align:center;padding:50px 20px;color:var(--muted)}.empty .symbol{font-size:45px;margin-bottom:10px}.detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:35px;align-items:center}.detail-photo{height:450px;border-radius:25px;background:var(--surface2);display:flex;align-items:center;justify-content:center;overflow:hidden}.detail-photo img{width:100%;height:100%;object-fit:contain}.detail-photo .no-photo{font-size:130px}.detail-title{font-size:clamp(32px,4vw,48px);letter-spacing:-1.8px;line-height:1.04;margin:10px 0}.qty{display:flex;align-items:center;gap:13px}.qty input{width:65px;text-align:center;border:1px solid var(--line);background:var(--surface);color:var(--text);padding:10px;border-radius:999px}.cart-item{display:flex;gap:14px;padding:14px 0;border-bottom:1px solid var(--line);align-items:center}.cart-item:last-of-type{border-bottom:0}.cart-item-photo{width:86px;height:86px;flex:0 0 86px;background:var(--surface2);border-radius:14px;display:grid;place-items:center;overflow:hidden}.cart-item-photo img{width:100%;height:100%;object-fit:cover}.cart-item-info{flex:1;min-width:0}.cart-item-name{display:block;margin-bottom:5px}.cart-item-subtotal{display:block;margin-top:7px}.cart-controls{display:flex;align-items:center;gap:8px;margin-top:9px}.cart-qty-btn{width:34px;height:34px;padding:0;border-radius:50%;background:var(--surface2);color:var(--text);border:1px solid var(--line);font-size:19px}.cart-qty-value{min-width:24px;text-align:center;font-weight:750}.cart-qty-btn:disabled{opacity:.35;cursor:not-allowed;transform:none}.cart-remove-form{margin-left:4px}.cart-remove-btn{width:38px;height:38px;padding:0;background:var(--surface2);color:var(--text);border:1px solid var(--line);font-size:20px}.cart-summary-total{font-size:20px}.mobile-bottom{display:none}.metric-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.metric{background:var(--surface2);padding:18px;border-radius:16px}.metric small{display:block;color:var(--muted);margin-bottom:8px}.metric strong{font-size:26px}.admin-side{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}.small{font-size:12px}\n@media(max-width:900px){.grid{grid-template-columns:repeat(3,minmax(0,1fr))}.navlinks{gap:12px}.search{min-width:150px}.hero{padding:35px}.hero h1{font-size:48px}.metric-grid{grid-template-columns:repeat(2,1fr)}}\n@media(max-width:650px){.wrap{width:92%}.nav{min-height:64px;flex-wrap:wrap;padding:10px 0}.navlinks{order:3;width:100%;overflow-x:auto;padding:2px 0 7px;gap:17px}.navlinks a{white-space:nowrap}.navactions .search{min-width:0;width:42px;padding:0;justify-content:center;border:0;background:transparent}.navactions .search input{display:none}.navactions .search span{font-size:19px}.hero{grid-template-columns:1fr;min-height:0;padding:30px 25px;margin-top:20px;border-radius:24px}.hero h1{font-size:43px;letter-spacing:-2px}.hero-art{min-height:150px}.hero-art img{height:180px}.hero-placeholder{font-size:100px}.grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:11px}.product-card{padding:8px;border-radius:17px}.product-photo{height:145px;border-radius:12px}.product-info{padding:10px 3px 4px}.product-info h3{font-size:14px}.price{font-size:16px}.heart{top:14px;right:14px;width:31px;height:31px}.section-head{align-items:start}.section-head h2{font-size:25px}.detail-grid,.two-col{grid-template-columns:1fr;gap:20px}.detail-photo{height:310px}.form-grid{grid-template-columns:1fr}.field.full{grid-column:auto}.panel{padding:16px;border-radius:18px}.mobile-bottom{display:flex;position:fixed;bottom:10px;left:4%;width:92%;z-index:12;background:var(--surface);border:1px solid var(--line);box-shadow:var(--shadow);border-radius:20px;justify-content:space-around;padding:9px}.mobile-bottom a{display:flex;flex-direction:column;align-items:center;gap:3px;font-size:10px;color:var(--muted)}.mobile-bottom b{font-size:19px;color:var(--text)}.footer{padding-bottom:95px}.metric-grid{grid-template-columns:repeat(2,1fr)}body{padding-bottom:65px}}\n\n/* Shopmobi premium design upgrade */\n.product-card {\n    transition: transform 0.25s ease, box-shadow 0.25s ease;\n}\n\n.product-card:hover {\n    transform: translateY(-7px);\n    box-shadow: 0 18px 40px rgba(18, 40, 24, 0.14);\n}\n\n.product-photo img {\n    transition: transform 0.35s ease;\n}\n\n.product-card:hover .product-photo img {\n    transform: scale(1.07);\n}\n\n.chip {\n    transition: all 0.2s ease;\n}\n\n.chip:hover {\n    background: var(--green);\n    color: white;\n    border-color: var(--green);\n}\n\n.section-head {\n    margin-top: 40px;\n}\n\n.product-info h3 {\n    transition: color 0.2s ease;\n}\n\n.product-info h3:hover {\n    color: var(--green2);\n}\n\n'
BASE = '<!doctype html><html lang="en" data-theme="light"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{ title }} · Shopmobi</title><style>\n:root{--bg:#f1f2ec;--surface:#fff;--surface2:#e7e9df;--text:#172018;--muted:#687267;--line:#dce1d7;--green:#173d2b;--green2:#2d6444;--accent:#a8c66c;--black:#080b09;--shadow:0 12px 35px rgba(18,40,24,.08)}\nhtml[data-theme="dark"]{--bg:#111a14;--surface:#1b2820;--surface2:#26362a;--text:#f0f5ed;--muted:#b1beb0;--line:#344638;--green:#c0df9a;--green2:#a8d081;--accent:#b7d98a;--black:#070b08;--shadow:0 12px 35px rgba(0,0,0,.22)}\n*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;transition:background .2s,color .2s}a{color:inherit;text-decoration:none}button,input,select,textarea{font:inherit}button,.btn{border:0;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:11px 16px;border-radius:999px;background:var(--black);color:white;font-weight:650;text-decoration:none;transition:transform .15s,opacity .15s}button:hover,.btn:hover{opacity:.88;transform:translateY(-1px)}.btn-light{background:var(--surface2);color:var(--text)}.btn-green{background:var(--green);color:#fff}.btn-danger{background:#a82c2c;color:#fff}.icon-btn{width:42px;height:42px;padding:0;background:var(--surface);color:var(--text);border:1px solid var(--line);font-size:18px}.wrap{width:min(1180px,92%);margin:auto}.topbar{position:sticky;top:0;z-index:10;background:color-mix(in srgb,var(--bg) 90%,transparent);backdrop-filter:blur(16px);border-bottom:1px solid var(--line)}.nav{min-height:76px;display:flex;align-items:center;justify-content:space-between;gap:20px}.logo{font-size:25px;font-weight:900;letter-spacing:-1.3px;color:var(--green)}.logo span{color:var(--text)}.navlinks{display:flex;align-items:center;gap:20px;color:var(--muted);font-size:14px}.navlinks a:hover{color:var(--text)}.navactions{display:flex;gap:8px;align-items:center}.search{display:flex;align-items:center;gap:9px;background:var(--surface);border:1px solid var(--line);border-radius:999px;padding:0 15px;min-width:210px}.search input{border:0;outline:0;background:transparent;color:var(--text);padding:12px 0;width:100%}.hero{margin:30px auto 36px;border-radius:30px;overflow:hidden;min-height:350px;background:radial-gradient(circle at 85% 10%,#678b51 0,transparent 34%),linear-gradient(115deg,#102c1e,#315b38 58%,#8eaa6b);color:#fff;display:grid;grid-template-columns:1.1fr .9fr;align-items:center;padding:48px clamp(25px,5vw,70px);position:relative}.hero small{display:inline-block;background:#ffffff1f;border:1px solid #ffffff30;padding:8px 13px;border-radius:999px;letter-spacing:.08em;text-transform:uppercase;font-size:11px}.hero h1{font-size:clamp(38px,5vw,65px);line-height:.99;letter-spacing:-3px;max-width:560px;margin:22px 0 15px}.hero p{max-width:440px;color:#e2eadd;line-height:1.65}.hero .btn{background:#f5f6ee;color:#15271a;margin-top:10px}.hero-art{display:flex;justify-content:center;align-items:center;min-height:240px}.hero-art img{width:min(100%,400px);height:280px;object-fit:contain;filter:drop-shadow(0 25px 20px #06150b60)}.hero-placeholder{font-size:140px;filter:drop-shadow(0 20px 12px #06150b55)}.section-head{display:flex;justify-content:space-between;align-items:end;gap:14px;margin:32px 0 18px}.section-head h2{font-size:clamp(24px,3vw,32px);letter-spacing:-1px;margin:0}.section-head p{margin:7px 0 0;color:var(--muted)}.chips{display:flex;gap:9px;overflow-x:auto;padding:2px 0 8px;margin-bottom:15px}.chip{white-space:nowrap;border:1px solid var(--line);border-radius:999px;padding:10px 16px;background:var(--surface);color:var(--text);font-size:13px}.chip.active{background:var(--black);color:#fff;border-color:var(--black)}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:18px}.product-card{background:var(--surface);border:1px solid var(--line);border-radius:22px;padding:12px;min-width:0;box-shadow:var(--shadow);position:relative;display:flex;flex-direction:column}.product-photo{height:205px;background:var(--surface2);border-radius:15px;overflow:hidden;display:flex;align-items:center;justify-content:center}.product-photo img{width:100%;height:100%;object-fit:cover}.no-photo{font-size:60px}.heart{position:absolute;top:21px;right:21px;width:36px;height:36px;border-radius:50%;background:var(--surface);color:var(--text);padding:0;border:1px solid var(--line)}.heart.on{color:#bc303d}.product-info{padding:13px 4px 4px;display:flex;flex-direction:column;gap:7px;flex:1}.product-info h3{margin:0;font-size:16px;letter-spacing:-.3px}.muted{color:var(--muted)}.product-meta{font-size:12px;color:var(--muted)}.price-row{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:auto;padding-top:7px}.price{font-weight:850;font-size:19px;letter-spacing:-.4px}.mini-add{width:36px;height:36px;padding:0;border-radius:50%;font-size:20px}.stock{font-size:12px;color:var(--muted)}.panel{background:var(--surface);border:1px solid var(--line);border-radius:22px;padding:22px;margin:18px 0;box-shadow:var(--shadow)}.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 16px}.field{margin-bottom:13px}.field.full{grid-column:1/-1}.field label{display:block;font-size:13px;font-weight:700;margin-bottom:7px}.field input,.field select,.field textarea{width:100%;border:1px solid var(--line);border-radius:12px;background:var(--bg);color:var(--text);padding:12px 13px;outline-color:var(--green2)}.field textarea{min-height:90px;resize:vertical}.notice{background:var(--surface2);border:1px solid var(--line);padding:13px 16px;border-radius:13px;margin:13px 0}.table-wrap{overflow-x:auto}.table-product-image{width:68px;height:68px;object-fit:cover;border-radius:12px;background:var(--surface2);display:block}.table-product-placeholder{width:68px;height:68px;border-radius:12px;background:var(--surface2);display:grid;place-items:center;font-size:25px}table{width:100%;border-collapse:collapse;min-width:650px}th,td{text-align:left;padding:13px 11px;border-bottom:1px solid var(--line);font-size:14px}th{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}.status{display:inline-block;border-radius:999px;padding:5px 9px;font-size:12px;background:var(--surface2)}.footer{margin-top:60px;padding:30px 0;border-top:1px solid var(--line);color:var(--muted);font-size:13px}.two-col{display:grid;grid-template-columns:1fr 1fr;gap:20px}.empty{text-align:center;padding:50px 20px;color:var(--muted)}.empty .symbol{font-size:45px;margin-bottom:10px}.detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:35px;align-items:center}.detail-photo{height:450px;border-radius:25px;background:var(--surface2);display:flex;align-items:center;justify-content:center;overflow:hidden}.detail-photo img{width:100%;height:100%;object-fit:contain}.detail-photo .no-photo{font-size:130px}.detail-title{font-size:clamp(32px,4vw,48px);letter-spacing:-1.8px;line-height:1.04;margin:10px 0}.qty{display:flex;align-items:center;gap:13px}.qty input{width:65px;text-align:center;border:1px solid var(--line);background:var(--surface);color:var(--text);padding:10px;border-radius:999px}.cart-item{display:flex;gap:14px;padding:14px 0;border-bottom:1px solid var(--line);align-items:center}.cart-item:last-of-type{border-bottom:0}.cart-item-photo{width:86px;height:86px;flex:0 0 86px;background:var(--surface2);border-radius:14px;display:grid;place-items:center;overflow:hidden}.cart-item-photo img{width:100%;height:100%;object-fit:cover}.cart-item-info{flex:1;min-width:0}.cart-item-name{display:block;margin-bottom:5px}.cart-item-subtotal{display:block;margin-top:7px}.cart-controls{display:flex;align-items:center;gap:8px;margin-top:9px}.cart-qty-btn{width:34px;height:34px;padding:0;border-radius:50%;background:var(--surface2);color:var(--text);border:1px solid var(--line);font-size:19px}.cart-qty-value{min-width:24px;text-align:center;font-weight:750}.cart-qty-btn:disabled{opacity:.35;cursor:not-allowed;transform:none}.cart-remove-form{margin-left:4px}.cart-remove-btn{width:38px;height:38px;padding:0;background:var(--surface2);color:var(--text);border:1px solid var(--line);font-size:20px}.cart-summary-total{font-size:20px}.mobile-bottom{display:none}.metric-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.metric{background:var(--surface2);padding:18px;border-radius:16px}.metric small{display:block;color:var(--muted);margin-bottom:8px}.metric strong{font-size:26px}.admin-side{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}.small{font-size:12px}\n@media(max-width:900px){.grid{grid-template-columns:repeat(3,minmax(0,1fr))}.navlinks{gap:12px}.search{min-width:150px}.hero{padding:35px}.hero h1{font-size:48px}.metric-grid{grid-template-columns:repeat(2,1fr)}}\n@media(max-width:650px){.wrap{width:92%}.nav{min-height:64px;flex-wrap:wrap;padding:10px 0}.navlinks{order:3;width:100%;overflow-x:auto;padding:2px 0 7px;gap:17px}.navlinks a{white-space:nowrap}.navactions .search{min-width:0;width:42px;padding:0;justify-content:center;border:0;background:transparent}.navactions .search input{display:none}.navactions .search span{font-size:19px}.hero{grid-template-columns:1fr;min-height:0;padding:30px 25px;margin-top:20px;border-radius:24px}.hero h1{font-size:43px;letter-spacing:-2px}.hero-art{min-height:150px}.hero-art img{height:180px}.hero-placeholder{font-size:100px}.grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:11px}.product-card{padding:8px;border-radius:17px}.product-photo{height:145px;border-radius:12px}.product-info{padding:10px 3px 4px}.product-info h3{font-size:14px}.price{font-size:16px}.heart{top:14px;right:14px;width:31px;height:31px}.section-head{align-items:start}.section-head h2{font-size:25px}.detail-grid,.two-col{grid-template-columns:1fr;gap:20px}.detail-photo{height:310px}.form-grid{grid-template-columns:1fr}.field.full{grid-column:auto}.panel{padding:16px;border-radius:18px}.mobile-bottom{display:flex;position:fixed;bottom:10px;left:4%;width:92%;z-index:12;background:var(--surface);border:1px solid var(--line);box-shadow:var(--shadow);border-radius:20px;justify-content:space-around;padding:9px}.mobile-bottom a{display:flex;flex-direction:column;align-items:center;gap:3px;font-size:10px;color:var(--muted)}.mobile-bottom b{font-size:19px;color:var(--text)}.footer{padding-bottom:95px}.metric-grid{grid-template-columns:repeat(2,1fr)}body{padding-bottom:65px}}\n\n/* Shopmobi premium design upgrade */\n.product-card {\n    transition: transform 0.25s ease, box-shadow 0.25s ease;\n}\n\n.product-card:hover {\n    transform: translateY(-7px);\n    box-shadow: 0 18px 40px rgba(18, 40, 24, 0.14);\n}\n\n.product-photo img {\n    transition: transform 0.35s ease;\n}\n\n.product-card:hover .product-photo img {\n    transform: scale(1.07);\n}\n\n.chip {\n    transition: all 0.2s ease;\n}\n\n.chip:hover {\n    background: var(--green);\n    color: white;\n    border-color: var(--green);\n}\n\n.section-head {\n    margin-top: 40px;\n}\n\n.product-info h3 {\n    transition: color 0.2s ease;\n}\n\n.product-info h3:hover {\n    color: var(--green2);\n}\n\n\n.product-buy-controls{display:flex;align-items:center;gap:22px;margin:20px 0;flex-wrap:wrap}\n.product-buy-controls input{width:78px;padding:12px;text-align:center;border:1px solid var(--line);border-radius:12px;background:var(--surface);color:var(--text)}\n.product-buy-controls button{min-width:145px}\n</style></head><body>\n<div class="topbar"><div class="wrap nav"><a class="logo" href="{{ url_for(\'home\') }}">shop<span>mobi.</span></a>\n<nav class="navlinks"><a href="{{ url_for(\'home\') }}">Discover</a><a href="{{ url_for(\'home\', category=\'Home & Living\') }}">Home & Living</a><a href="{{ url_for(\'home\', category=\'Fashion\') }}">Fashion</a><a href="{{ url_for(\'merchant_register\') }}">Sell with us</a></nav>\n<div class="navactions"><form class="search" method="get" action="{{ url_for(\'home\') }}"><span>⌕</span><input name="q" value="{{ request.args.get(\'q\',\'\') }}" placeholder="Search products"></form><a class="btn btn-light" href="{{ url_for(\'cart\') }}" aria-label="Cart">♧ <span>{{ cart_count }}</span></a><button class="icon-btn" id="themeToggle" type="button" aria-label="Toggle night mode">☾</button></div></div></div>\n<main class="wrap">{% with messages=get_flashed_messages() %}{% for message in messages %}<div class="notice">{{ message }}</div>{% endfor %}{% endwith %}{{ content|safe }}</main>\n<div class="mobile-bottom"><a href="{{ url_for(\'home\') }}"><b>⌂</b>Shop</a><a href="{{ url_for(\'cart\') }}"><b>♧</b>Cart</a><a href="{{ url_for(\'merchant_login\') }}"><b>♙</b>Seller</a><a href="{{ url_for(\'admin_login\') }}"><b>⚙</b>Admin</a></div>\n\n<script>\n(function(){const root=document.documentElement;let saved=\'light\';try{saved=localStorage.getItem(\'shopmobi-theme\')||\'light\'}catch(e){}root.dataset.theme=saved;const btn=document.getElementById(\'themeToggle\');function update(){if(btn)btn.textContent=root.dataset.theme===\'dark\'?\'☀\':\'☾\';}update();if(btn)btn.addEventListener(\'click\',()=>{root.dataset.theme=root.dataset.theme===\'dark\'?\'light\':\'dark\';try{localStorage.setItem(\'shopmobi-theme\',root.dataset.theme)}catch(e){}update()});})();\n</script></body></html>'

if __name__ == '__main__':
    app.run(debug=False)

