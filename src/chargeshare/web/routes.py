"""管委會與住戶的網頁路由。"""

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from chargeshare.db.models import Bill, BillingRun, Household, Transaction
from chargeshare.security import verify_password
from chargeshare.services.billing import set_bill_paid
from chargeshare.web import views

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
TEMPLATES.env.filters.update(kwh=views.kwh, money=views.money, local_time=views.local_time)

router = APIRouter()


def get_session(request: Request):
    with request.app.state.ctx.session_factory.begin() as session:
        yield session


def require_admin(request: Request) -> None:
    if not request.session.get("admin"):
        raise HTTPException(303, headers={"Location": "/admin/login"})


def require_household(request: Request, session: Session = Depends(get_session)) -> Household:
    household_id = request.session.get("household_id")
    household = session.get(Household, household_id) if household_id else None
    if household is None:
        raise HTTPException(303, headers={"Location": "/login"})
    return household


def redirect(location: str) -> RedirectResponse:
    return RedirectResponse(location, status_code=303)


# ── 登入 ───────────────────────────────────────────────────────────


@router.get("/")
def index() -> RedirectResponse:
    return redirect("/login")


@router.get("/login", response_class=HTMLResponse)
def resident_login_form(request: Request):
    return TEMPLATES.TemplateResponse(request, "login.html", {"role": "resident"})


@router.post("/login")
def resident_login(
    request: Request,
    unit_no: str = Form(),
    password: str = Form(),
    session: Session = Depends(get_session),
):
    household = session.scalar(select(Household).where(Household.unit_no == unit_no.strip()))
    if household is None or not verify_password(password, household.password_hash):
        context = {"role": "resident", "error": "戶號或密碼錯誤", "unit_no": unit_no}
        return TEMPLATES.TemplateResponse(request, "login.html", context, status_code=401)
    request.session.clear()
    request.session["household_id"] = household.id
    return redirect("/me")


@router.get("/admin/login", response_class=HTMLResponse)
def admin_login_form(request: Request):
    return TEMPLATES.TemplateResponse(request, "login.html", {"role": "admin"})


@router.post("/admin/login")
def admin_login(request: Request, username: str = Form(), password: str = Form()):
    settings = request.app.state.ctx.settings
    if (username, password) != (settings.admin_username, settings.admin_password):
        context = {"role": "admin", "error": "帳號或密碼錯誤"}
        return TEMPLATES.TemplateResponse(request, "login.html", context, status_code=401)
    request.session.clear()
    request.session["admin"] = True
    return redirect("/admin")


@router.get("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return redirect("/login")


# ── 管委會 ─────────────────────────────────────────────────────────


@router.get("/admin", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def admin_dashboard(request: Request, session: Session = Depends(get_session)):
    ctx = request.app.state.ctx
    context = {
        "chargers": views.charger_views(session, ctx.power),
        "capacity_kw": views.kw(ctx.settings.power.capacity_w),
    }
    return TEMPLATES.TemplateResponse(request, "admin/dashboard.html", context)


@router.get("/admin/chargers", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def admin_chargers(request: Request, session: Session = Depends(get_session)):
    chargers = views.charger_views(session, request.app.state.ctx.power)
    return TEMPLATES.TemplateResponse(request, "admin/_chargers.html", {"chargers": chargers})


@router.post("/admin/chargers/{charger_id}/release", dependencies=[Depends(require_admin)])
def admin_release(request: Request, charger_id: str):
    try:
        request.app.state.ctx.power.release_reservation(charger_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return redirect("/admin")


@router.get("/api/power", dependencies=[Depends(require_admin)])
def power_series(request: Request, session: Session = Depends(get_session)) -> JSONResponse:
    ctx = request.app.state.ctx
    return JSONResponse(
        {
            "capacity_w": ctx.settings.power.capacity_w,
            "points": views.power_points(session, ctx.power, ctx.clock.now()),
        }
    )


@router.get("/admin/bills", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def admin_bills(
    request: Request, period: str | None = None, session: Session = Depends(get_session)
):
    periods = list(session.scalars(select(BillingRun.period).order_by(BillingRun.period.desc())))
    period = period or (periods[0] if periods else None)
    bills = session.scalars(
        select(Bill).join(Household).where(Bill.period == period).order_by(Household.unit_no)
    ).all()
    context = {"bills": bills, "period": period, "periods": periods}
    return TEMPLATES.TemplateResponse(request, "admin/bills.html", context)


@router.get(
    "/admin/bills/{bill_id}", response_class=HTMLResponse, dependencies=[Depends(require_admin)]
)
def admin_bill_detail(request: Request, bill_id: int, session: Session = Depends(get_session)):
    bill = session.get(Bill, bill_id)
    if bill is None:
        raise HTTPException(404)
    return TEMPLATES.TemplateResponse(
        request, "bill_detail.html", {"bill": bill, "back": "/admin/bills"}
    )


@router.post(
    "/admin/bills/{bill_id}/paid",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin)],
)
def admin_set_paid(
    request: Request, bill_id: int, paid: bool = Form(), session: Session = Depends(get_session)
):
    try:
        bill = set_bill_paid(session, bill_id, paid, request.app.state.ctx.clock.now())
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return TEMPLATES.TemplateResponse(request, "admin/_bill_row.html", {"bill": bill})


# ── 住戶 ───────────────────────────────────────────────────────────


@router.get("/me", response_class=HTMLResponse)
def resident_home(
    request: Request,
    household: Household = Depends(require_household),
    session: Session = Depends(get_session),
):
    txs = session.scalars(
        select(Transaction)
        .where(Transaction.household_id == household.id)
        .order_by(Transaction.start_ts.desc())
        .limit(100)
    ).all()
    bills = session.scalars(
        select(Bill).where(Bill.household_id == household.id).order_by(Bill.period.desc())
    ).all()
    context = {"household": household, "transactions": txs, "bills": bills}
    return TEMPLATES.TemplateResponse(request, "resident/home.html", context)


@router.get("/me/bills/{bill_id}", response_class=HTMLResponse)
def resident_bill(
    request: Request,
    bill_id: int,
    household: Household = Depends(require_household),
    session: Session = Depends(get_session),
):
    bill = session.get(Bill, bill_id)
    if bill is None or bill.household_id != household.id:
        raise HTTPException(404)
    return TEMPLATES.TemplateResponse(request, "bill_detail.html", {"bill": bill, "back": "/me"})
