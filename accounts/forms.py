"""Fields a signed-in user can edit on their own personal information page."""
from django import forms
from django.contrib.auth import get_user_model


class PersonalInfoForm(forms.ModelForm):
    class Meta:
        model = get_user_model()
        fields = ('last_name', 'first_name', 'email')
        labels = {'last_name': '姓氏', 'first_name': '名字', 'email': '联系邮箱'}
        widgets = {
            'last_name': forms.TextInput(attrs={'autocomplete': 'family-name'}),
            'first_name': forms.TextInput(attrs={'autocomplete': 'given-name'}),
            'email': forms.EmailInput(attrs={'autocomplete': 'email'}),
        }
