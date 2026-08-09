"""accounts views: 注册（登录/登出用 Django 内建）"""
from django.contrib.auth import login
from django.contrib.auth.forms import UserCreationForm
from django.shortcuts import redirect, render


def register_view(request):
    """用户注册。"""
    if request.user.is_authenticated:
        return redirect("/")
    if request.method == "POST":
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect("/")
    else:
        form = UserCreationForm()
    return render(request, "accounts/register.html", {"form": form})
